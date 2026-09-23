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
  cpSync(join(repository, 'examples', 'web-scenarios', 'bounded-long-slippage.yaml'), join(scenarioRoot, 'bounded-long-slippage.yaml'))
  writeFileSync(join(scenarioRoot, 'one.yaml'), bounded.replace("target_quantity: '2'", "target_quantity: '1'"))
  writeFileSync(join(scenarioRoot, 'low-cash.yaml'), bounded.replace("initial_cash: '10000'", "initial_cash: '50'"))
  writeFileSync(join(scenarioRoot, 'invalid.yaml'), bounded.replace(/sha256: [0-9a-f]{64}/, `sha256: ${'0'.repeat(64)}`))

  for (const name of ['bounded-long.yaml', 'moving-average-entry.yaml', 'bounded-long-slippage.yaml']) {
    execFileSync(ea, ['backtest', 'validate', '--scenario', join(scenarioRoot, name)], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
    execFileSync(ea, ['backtest', 'run', '--scenario', join(scenarioRoot, name), '--output-root', join(temporary, 'cli-runs')], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
  }

  const strategyRoot = join(temporary, 'strategies')
  const strategySource = join(outside, 'my-strategy')
  mkdirSync(strategyRoot)
  mkdirSync(strategySource)
  // The reference implementation is created only in the external acceptance workspace.
  execFileSync(python, ['-I', '-c', `
import json, pathlib, sys, yaml
source, scenarios = map(pathlib.Path, sys.argv[1:])
manifest = {"schema_version": 1, "package_id": "example.threshold", "strategy": {
 "id": "local-close-threshold-entry-v1", "version": 1, "display_name": "Local threshold",
 "outcome_mode": "optional_single_long_entry", "parameters": [
 {"name": name, "type": "decimal", "required": True, "default": value,
  "static_minimum": "0", "static_maximum": None}
 for name, value in [("threshold_price", "100"), ("target_quantity", "2")]]}}
(source / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
(source / "strategy.py").write_text("""from decimal import Decimal
from ea.strategy.sdk_v1 import StrategyDecisionV1
def validate_parameters(parameters, context):
    if Decimal(parameters['target_quantity']) <= 0: raise ValueError('quantity')
class Logic:
    def __init__(self, p): self.p = p
    def on_bar(self, bar):
        if bar.close > Decimal(self.p['threshold_price']):
            return StrategyDecisionV1(self.p['target_quantity'])
def create_logic(parameters): return Logic(parameters)
""")
`, strategySource, scenarioRoot], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
  const artifact = join(strategyRoot, 'threshold.eastrategy')
  execFileSync(ea, ['strategy', 'pack', '--source', strategySource, '--output', artifact], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
  execFileSync(ea, ['strategy', 'validate', '--artifact', artifact], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
  execFileSync(ea, ['strategy', 'inspect', '--artifact', artifact], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
  execFileSync(python, ['-I', '-c', `
import hashlib, pathlib, sys, yaml
root, artifact = map(pathlib.Path, sys.argv[1:])
for original, name in [('bounded-long.yaml', 'local-threshold.yaml'), ('chronological-holdout.yaml', 'local-holdout.yaml')]:
    doc = yaml.safe_load((root / original).read_text())
    doc['schema_version'] = 3
    doc['execution'].pop('commission', None)
    doc['strategy'] = {'id': 'local-close-threshold-entry-v1', 'version': 1,
       'source': {'kind': 'local-package', 'package_id': 'example.threshold',
                  'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()},
       'parameters': {'threshold_price': '100', 'target_quantity': '2'}}
    (root / name).write_text(yaml.safe_dump(doc))
`, scenarioRoot, artifact], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
  execFileSync(ea, ['backtest', 'validate', '--scenario', join(scenarioRoot, 'local-threshold.yaml'), '--strategy-root', strategyRoot], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
  execFileSync(ea, ['backtest', 'run', '--scenario', join(scenarioRoot, 'local-threshold.yaml'), '--strategy-root', strategyRoot, '--output-root', join(temporary, 'cli-runs')], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })

  const dataRoot = join(temporary, 'research-data')
  mkdirSync(dataRoot)
  cpSync(join(scenarioRoot, 'prices.csv'), join(dataRoot, 'research.csv'))
  cpSync(join(scenarioRoot, 'holdout-prices.csv'), join(dataRoot, 'later.csv'))
  for (const [source, target] of [['moving-average-entry.csv', 'roundtrip.csv'], ['moving-average-holdout.csv', 'roundtrip-later.csv']]) cpSync(join(scenarioRoot, source), join(dataRoot, target))
  cpSync(join(repository, 'examples', 'web-scenarios', 'single-long-round-trip.yaml'), join(scenarioRoot, 'roundtrip.yaml'))
  execFileSync(ea, ['data', 'inspect', join(dataRoot, 'research.csv')], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })

  const v2Source = join(outside, 'ma-crossover-v2')
  cpSync(join(repository, 'examples/local-strategies/moving-average-crossover-v2'), v2Source, { recursive: true })
  const v2Artifact = join(strategyRoot, 'ma-v2.eastrategy')
  execFileSync(ea, ['strategy', 'pack', '--source', v2Source, '--output', v2Artifact], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
  for (const command of ['validate', 'inspect']) execFileSync(ea, ['strategy', command, '--artifact', v2Artifact], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
  execFileSync(python, ['-I', join(webRoot, 'scripts/setup-local-v2.py'), scenarioRoot, dataRoot, v2Artifact], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })

  const v3Source = join(outside, 'ma-crossover-v3')
  cpSync(join(repository, 'examples/local-strategies/moving-average-crossover-v3'), v3Source, { recursive: true })
  const v3Artifact = join(strategyRoot, 'ma-v3.eastrategy')
  execFileSync(ea, ['strategy', 'pack', '--source', v3Source, '--output', v3Artifact], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
  for (const command of ['validate', 'inspect']) execFileSync(ea, ['strategy', command, '--artifact', v3Artifact], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
  execFileSync(python, ['-I', join(webRoot, 'scripts/setup-local-v3.py'), scenarioRoot, dataRoot, v3Artifact, join(repository, 'examples/research-data/coinbase-btc-usd-2024.json')], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })
  execFileSync(ea, ['data', 'inspect', join(dataRoot, 'local-v3.csv')], { cwd: outside, stdio: 'inherit', env: cleanEnvironment() })

  const args = ['web', 'serve', '--data-root', dataRoot, '--strategy-root', strategyRoot, '--scenario-root', scenarioRoot, '--workspace', workspace, '--ui-dir', join(webRoot, 'dist'), '--port', port]
  server = spawn(ea, args, { cwd: outside, env: cleanEnvironment(), stdio: ['ignore', 'inherit', 'inherit'] })
  await waitForHealth()
  console.log(`candidate-wheel-sha256=${sha256(wheel)} file=${basename(wheel)}`)
  const distIdentity = createHash('sha256')
  for (const file of files(join(webRoot, 'dist'))) distIdentity.update(`${file}\0${sha256(join(webRoot, 'dist', file))}\n`)
  console.log(`web-dist-manifest-sha256=${distIdentity.digest('hex')}`)

  const playwright = join(webRoot, 'node_modules', '.bin', 'playwright')
  const completed = spawn(playwright, ['test', '--config', 'playwright.config.ts', ...(process.env.EA_WEB_E2E_GREP ? ['--grep', process.env.EA_WEB_E2E_GREP] : [])], {
    cwd: webRoot,
    env: { ...cleanEnvironment(), EA_WEB_BASE_URL: baseURL, EA_WEB_SERVER_PID: String(server.pid), EA_WEB_BIN: ea, EA_WEB_ARGS: JSON.stringify(args), EA_WEB_WORKSPACE: workspace, EA_STRATEGY_ARTIFACT: artifact, EA_DATA_ROOT: dataRoot, EA_V2_ARTIFACT: v2Artifact, EA_V2_SOURCE: v2Source, EA_V3_ARTIFACT: v3Artifact, EA_V3_SOURCE: v3Source, EA_SCENARIO_ROOT: scenarioRoot },
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
