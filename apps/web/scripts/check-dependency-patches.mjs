/**
 * Where the three dependency patches this stage installs have to hold, checked
 * against what is actually installed in `node_modules`.
 *
 * Version evidence and behaviour evidence are kept apart on purpose. The floors
 * are the versions the fixed patches published, and each parent's own range is
 * read from its installed `package.json` instead of assumed, so a tree that kept
 * an older or nested copy fails here. The behaviour smoke at the end says nothing
 * about a CVE and reproduces none of them: it only shows the ordinary paths this
 * app and its test tooling actually use still work, on bounded inputs, with
 * nothing addressed outside this host and no socket opened on the scripted path.
 *
 * Run it before and after installing the patches. It fails on the old versions,
 * and the behaviour cases only run once every floor is met, so an unpatched tree
 * never reaches them.
 */
import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')

/**
 * name: the patched package.
 * floor: the version the fix published; every installed copy has to reach it.
 * parents: each installed dependent, with the range its own package.json declares.
 */
const PATCHES = [
  {
    name: 'brace-expansion',
    floor: '5.0.11',
    advisories: 'GHSA-qhr7-859c-m2p7 CVE-2026-102278 GHSA-6j4f-fj2g-mc7p CVE-2026-102276',
    parents: [{ name: 'minimatch', range: '^5.0.8' }],
  },
  {
    name: 'undici',
    floor: '7.29.1',
    advisories: 'GHSA-rfgv-xxqx-mfg5 CVE-2026-19534 GHSA-w293-vg96-wgc3 CVE-2026-84961',
    parents: [{ name: 'jsdom', range: '^7.24.3' }],
  },
  {
    name: 'source-map-js',
    floor: '1.2.2',
    advisories: 'GHSA-68fv-2mgg-jv7q CVE-2026-93749',
    parents: [{ name: 'postcss', range: '^1.2.1' }, { name: 'css-tree', range: '^1.2.1' }],
  },
]

const problems = []
const problem = (message) => { problems.push(message); console.error(`problem: ${message}`) }
const readJson = (path) => JSON.parse(readFileSync(path, 'utf8'))

function version(text, what) {
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(String(text ?? ''))
  if (!match) throw new Error(`${what} is not a plain version: ${JSON.stringify(text)}`)
  return match.slice(1).map(Number)
}
const atLeast = (found, floor) => {
  for (let index = 0; index < 3; index += 1) if (found[index] !== floor[index]) return found[index] > floor[index]
  return true
}
/** Caret semantics for the majors these parents use: same major, at or above the base. */
const withinCaret = (installed, range) => {
  const base = version(range.replace(/^\^/, ''), `range ${range}`)
  const found = version(installed, 'an installed version')
  return found[0] === base[0] && atLeast(found, base)
}

/** The package.json Node itself would reach for `name` when resolving from `from`. */
function nearestPackageJson(from, name) {
  for (let directory = from; ;) {
    const candidate = join(directory, 'node_modules', name, 'package.json')
    if (existsSync(candidate)) return candidate
    const up = dirname(directory)
    if (up === directory) return null
    directory = up
  }
}

for (const patch of PATCHES) {
  const copies = new Map()
  const own = nearestPackageJson(webRoot, patch.name)
  if (own) copies.set(own, 'the app itself')
  else problem(`${patch.name} is not installed under ${webRoot}`)

  for (const parent of patch.parents) {
    const parentEntry = nearestPackageJson(webRoot, parent.name)
    if (!parentEntry) { problem(`${parent.name} is not installed, so ${patch.name} cannot be checked through it`); continue }
    // The floor was checked against this parent range. If the installed parent
    // now declares something else, that check has to be made again, not assumed.
    const declared = readJson(parentEntry).dependencies?.[patch.name]
    if (declared !== parent.range) problem(`${parent.name} declares ${patch.name} ${JSON.stringify(declared)}, not ${parent.range}`)
    const reachable = nearestPackageJson(dirname(parentEntry), patch.name)
    if (reachable) copies.set(reachable, `resolved from ${parent.name}`)
    else problem(`${parent.name} resolves no ${patch.name}`)
  }

  for (const [file, via] of copies) {
    const installed = readJson(file).version
    const ok = atLeast(version(installed, patch.name), version(patch.floor, 'the floor'))
      && patch.parents.every(parent => withinCaret(installed, parent.range))
    console.log(`${patch.name}=${installed} floor=${patch.floor} via="${via}" advisories="${patch.advisories}" ${ok ? 'ok' : 'BELOW-FLOOR'}`)
    if (!ok) problem(`${patch.name}@${installed} (${via}) is below ${patch.floor} or outside a parent range`)
  }
}

if (problems.length > 0) {
  console.error(`dependency-patches=failed problems=${problems.length}`)
  process.exit(1)
}

const check = (condition, message) => { if (!condition) problem(message) }

// Ordinary expansion, in this process: a brace list is a brace list.
const brace = await import('brace-expansion')
const expand = brace.expand ?? brace.default
const ordinary = expand('a{b,c}d')
check(Array.isArray(ordinary) && ordinary.join(',') === 'abd,acd', `ordinary brace expansion returned ${JSON.stringify(ordinary)}`)

// The two shapes this app's globs and ignore rules actually produce, on inputs
// this script bounds itself: ten concatenated groups are 1024 strings, and one
// nested group is a handful. This is an ordinary combinatorial smoke on bounded
// input, not a reproduction of any advisory — it shows the expansion path still
// works, and it says nothing about whether a specific exploit is or is not
// possible on any version. A child process with a timeout keeps a regression here
// from becoming this script's own hang.
const specifier = (() => {
  try { return import.meta.resolve('brace-expansion') } catch { return 'brace-expansion' }
})()
const child = spawnSync(process.execPath, ['--input-type=module', '-e', `
const mod = await import(${JSON.stringify(specifier)})
const expand = mod.expand ?? mod.default
console.log('expanded=' + expand('{a,b}'.repeat(10)).length)
console.log('nested=' + expand('{a,{b,c}}').length)
`], { cwd: webRoot, timeout: 10_000, encoding: 'utf8' })
check(child.status === 0, `a bounded brace pattern did not expand in a child process: ${child.error?.message ?? ((child.stderr ?? '').trim() || `status ${child.status}`)}`)
check((child.stdout ?? '').includes('expanded=1024'), `ten bounded brace groups expanded to ${(child.stdout ?? '').trim() || 'nothing'}`)
check(/nested=\d+/.test(child.stdout ?? ''), `a nested bounded brace group did not expand: ${(child.stdout ?? '').trim() || 'nothing'}`)

// A source map still round-trips the position it was built from.
const maps = await import('source-map-js')
const { SourceMapGenerator, SourceMapConsumer } = maps.default ?? maps
const generated = new SourceMapGenerator({ file: 'out.js' })
generated.addMapping({ generated: { line: 1, column: 0 }, original: { line: 7, column: 4 }, source: 'idea.ts' })
const consumer = await new SourceMapConsumer(generated.toString())
const position = consumer.originalPositionFor({ line: 1, column: 0 })
check(position.source === 'idea.ts' && position.line === 7 && position.column === 4, `a source map round trip returned ${JSON.stringify(position)}`)
consumer.destroy?.()

// The DOM the component tests run in still builds a document, with no resources
// and no request of any kind.
const { JSDOM } = await import('jsdom')
const dom = new JSDOM('<!doctype html><title>probe</title><p id="probe">ok</p>')
check(dom.window.document.getElementById('probe')?.textContent === 'ok', 'jsdom did not build the document it was given')
dom.window.close()

// BalancedPool has to keep function-valued options: a custom connector is the
// observable case the advisory names. The connector here is scripted and calls
// back with an error, so the run that reaches it opens no socket at all; only the
// unpatched shape, which drops the option, would try the loopback address, and
// nothing outside this host is ever addressed. The abort signal belongs in the
// request's own options — the second argument is a response handler, not a signal
// — and it bounds the attempt either way.
const { BalancedPool } = await import('undici')
if (typeof BalancedPool !== 'function') problem('undici exports no BalancedPool, so the connector case cannot be checked')
else {
  let connectorCalls = 0
  const pool = new BalancedPool(['http://127.0.0.1:9'], {
    connect: (...args) => {
      connectorCalls += 1
      const callback = args.find(argument => typeof argument === 'function')
      if (callback) callback(new Error('this check opens no connection'))
    },
  })
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 5000)
  try {
    await pool.request({ path: '/', method: 'GET', signal: controller.signal })
  } catch { /* the scripted connector above, or a refused loopback connect if the option was dropped */ }
  clearTimeout(timer)
  try { await pool.destroy() } catch { /* the pool is finished with either way */ }
  console.log(`undici-connect-option=${connectorCalls > 0 ? 'reached-the-connector' : 'dropped'}`)
  check(connectorCalls > 0, 'BalancedPool dropped a function-valued connector option instead of passing it to its pool')
}

if (problems.length > 0) {
  console.error(`dependency-patches=failed problems=${problems.length}`)
  process.exit(1)
}
console.log('dependency-patches=ok')
