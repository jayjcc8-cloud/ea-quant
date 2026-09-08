import { createHash } from 'node:crypto'
import { execFileSync, spawn } from 'node:child_process'
import { cpSync, existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { basename, dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repository = resolve(webRoot, '../..')
const temporary = mkdtempSync(join(tmpdir(), 'ea-web-e2e-'))
const wheelDirectory = join(temporary, 'wheel')
const environment = join(temporary, 'environment')
const outside = join(temporary, 'outside')
const scenarioRoot = join(temporary, 'scenarios')
const workspace = join(temporary, 'workspace')
const port = '8765'
const baseURL = `http://127.0.0.1:${port}`
let server

function cleanEnvironment() {
  const next = { ...process.env }
  delete next.PYTHONPATH
  delete next.VIRTUAL_ENV
  return next
}

function sha256(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex')
}

function files(root, prefix = '') {
  return readdirSync(join(root, prefix), { withFileTypes: true }).flatMap((entry) => {
    const relative = join(prefix, entry.name)
    return entry.isDirectory() ? files(root, relative) : [relative]
  }).sort()
}

function printFailureEvidence() {
  const jobs = join(workspace, 'jobs')
  if (!existsSync(jobs)) return
  for (const name of readdirSync(jobs).filter((item) => item.endsWith('.json')).sort()) {
    const payload = readFileSync(join(jobs, name), 'utf8')
    console.error(`e2e-job=${payload.trim()}`)
    const job = JSON.parse(payload)
    if (job.engine_run_id) {
      const failure = join(workspace, 'runs', job.engine_run_id, 'failure.json')
      if (existsSync(failure)) console.error(`e2e-engine-failure=${readFileSync(failure, 'utf8').trim()}`)
    }
  }
}

async function waitForHealth() {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    try {
      const response = await fetch(`${baseURL}/api/health`)
      if (response.ok) return
    } catch {}
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 50))
  }
  throw new Error('installed local Web service did not become ready')
}

try {
  mkdirSync(wheelDirectory)
  mkdirSync(outside)
  mkdirSync(scenarioRoot)
  execFileSync('uv', ['build', '--wheel', '--out-dir', wheelDirectory], { cwd: repository, stdio: 'inherit' })
  const wheels = readdirSync(wheelDirectory).filter((name) => name.endsWith('.whl'))
  if (wheels.length !== 1) throw new Error(`expected one candidate wheel, found ${wheels.length}`)
  const wheel = join(wheelDirectory, wheels[0])
  execFileSync('uv', ['venv', '--python', '3.12', environment], { cwd: outside, stdio: 'inherit' })
  const python = join(environment, 'bin', 'python')
  const ea = join(environment, 'bin', 'ea')
  execFileSync('uv', ['pip', 'install', '--python', python, `${wheel}[web]`], { cwd: outside, stdio: 'inherit' })
  execFileSync(python, ['-I', '-c', "import ea, pathlib; print(pathlib.Path(ea.__file__).resolve()); assert 'site-packages' in str(pathlib.Path(ea.__file__).resolve())"], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })

  cpSync(join(repository, 'examples', 'web-scenarios', 'prices.csv'), join(scenarioRoot, 'prices.csv'))
  for (const file of ['holdout-prices.csv', 'chronological-holdout.yaml', 'moving-average-entry.csv', 'moving-average-entry.yaml', 'moving-average-holdout.csv', 'moving-average-holdout.yaml']) cpSync(join(repository, 'examples', 'web-scenarios', file), join(scenarioRoot, file))
  const bounded = readFileSync(join(repository, 'examples', 'web-scenarios', 'bounded-long.yaml'), 'utf8')
  const flat = readFileSync(join(repository, 'examples', 'web-scenarios', 'flat.yaml'), 'utf8')
  writeFileSync(join(scenarioRoot, 'bounded-long.yaml'), bounded)
  writeFileSync(join(scenarioRoot, 'flat.yaml'), flat)
  cpSync(join(repository, 'examples', 'web-scenarios', 'bounded-long-commission.yaml'), join(scenarioRoot, 'bounded-long-commission.yaml'))
  writeFileSync(join(scenarioRoot, 'one.yaml'), bounded.replace("target_quantity: '2'", "target_quantity: '1'"))
  writeFileSync(join(scenarioRoot, 'low-cash.yaml'), bounded.replace("initial_cash: '10000'", "initial_cash: '50'"))
  writeFileSync(join(scenarioRoot, 'invalid.yaml'), bounded.replace(/sha256: [0-9a-f]{64}/, `sha256: ${'0'.repeat(64)}`))

  for (const name of ['bounded-long.yaml', 'moving-average-entry.yaml']) {
    execFileSync(ea, ['backtest', 'validate', '--scenario', join(scenarioRoot, name)], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
    execFileSync(ea, ['backtest', 'run', '--scenario', join(scenarioRoot, name), '--output-root', join(temporary, 'cli-runs')], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
  }

  const args = ['web', 'serve', '--scenario-root', scenarioRoot, '--workspace', workspace, '--ui-dir', join(webRoot, 'dist'), '--port', port]
  server = spawn(ea, args, { cwd: outside, env: cleanEnvironment(), stdio: ['ignore', 'inherit', 'inherit'] })
  await waitForHealth()
  console.log(`candidate-wheel-sha256=${sha256(wheel)} file=${basename(wheel)}`)
  const distIdentity = createHash('sha256')
  for (const file of files(join(webRoot, 'dist'))) distIdentity.update(`${file}\0${sha256(join(webRoot, 'dist', file))}\n`)
  console.log(`web-dist-manifest-sha256=${distIdentity.digest('hex')}`)

  const playwright = join(webRoot, 'node_modules', '.bin', 'playwright')
  const completed = spawn(playwright, ['test', '--config', 'playwright.config.ts'], {
    cwd: webRoot,
    env: { ...cleanEnvironment(), EA_WEB_BASE_URL: baseURL, EA_WEB_SERVER_PID: String(server.pid), EA_WEB_BIN: ea, EA_WEB_ARGS: JSON.stringify(args), EA_WEB_WORKSPACE: workspace },
    stdio: 'inherit',
  })
  const code = await new Promise((resolveExit) => completed.on('exit', resolveExit))
  if (code !== 0) {
    printFailureEvidence()
    process.exitCode = typeof code === 'number' ? code : 1
  }
} finally {
  if (server?.pid) {
    try { process.kill(server.pid, 'SIGTERM') } catch {}
  }
  if (!process.env.EA_WEB_E2E_KEEP) rmSync(temporary, { recursive: true, force: true })
  else console.log(`EA_WEB_E2E_KEEP=${temporary}`)
}
