# Dependency patches, 2026-10-06

Three packages in the local web toolchain are moved to their fixed releases in
`apps/web/package-lock.json`. Nothing else about the dependency graph changes: no `package.json`
edit, no new dependency, no reinstall performed by the writer, no forced or broad resolution, and
the same parent ranges, dependency lists and Node engine fields as before.

Baseline: `866a1fea4f0f6c8400c250b619c32f53d23abe5f` (`test: restore audit and plugin rejection fixture
coverage`). `apps/web/package-lock.json` on this branch was that accepted lock, unchanged, until this
stage, and the only change to it is the three entries below. `apps/web/package.json` is **not** edited:
no new dependency, no range change, no reinstall by the writer, no forced or broad resolution, and the
same parent ranges, dependency lists and Node engine fields as before. In each of the three lock
entries `version`, `resolved` and `integrity` move to the published tarball and integrity of the fixed
release; the license, the engine field and the dependency list of each entry are untouched.

The `dev` flag is left exactly as npm recorded it. It is `true` for `brace-expansion` and `undici`, and
`source-map-js` carries **no** `dev` flag at all — vite depends on it as a root (production)
dependency — even though EA's own use of it is the build/test CSS pipeline rather than the served app.

| package | before | fixed | reachable from | parent range (unchanged) |
| --- | --- | --- | --- | --- |
| `brace-expansion` | 5.0.9 | **5.0.11** | Node-side tooling only, through `minimatch` (the build/lint/test chain); never part of the served bundle | `minimatch` `^5.0.8` |
| `undici` | 7.29.0 | **7.29.1** | Dev/test only, through `jsdom`, which is the DOM the component tests run in | `jsdom` `^7.24.3` |
| `source-map-js` | 1.2.1 | **1.2.2** | Build tooling through `postcss` and dev tooling through `css-tree`, in the CSS pipeline | `postcss` `^1.2.1`, `css-tree` `^1.2.1` |

Each parent range above still admits the fixed release (`^5.0.8` admits 5.0.11, `^7.24.3` admits
7.29.1, `^1.2.1` admits 1.2.2), which is why a lock-only change is sufficient here. That the
installed parent still declares exactly the range recorded above is checked at run time rather than
assumed, by `apps/web/scripts/check-dependency-patches.mjs`.

What the direct source check behind the reachability column found, and what it did not establish: no
application code uses `undici` directly and no `BalancedPool` use exists in `apps/web` — jsdom is what
imports undici, for its `WebSocket` support — and `source-map-js` has no entry in this repository's
CSV or holdout data. That is an inference from reading the direct sources and the resolved lock, not a
proof that no reachable path to either package exists anywhere in the tree.

## The five advisories

Three packages, five advisories. The version resolved in this repository before the change is inside
the affected range of each; the fixed release is the one recorded above.

| advisory (GHSA · CVE) | package | affected range | first patched | resolved here before |
| --- | --- | --- | --- | --- |
| [GHSA-qhr7-859c-m2p7](https://github.com/advisories/GHSA-qhr7-859c-m2p7) · [CVE-2026-102278](https://www.cve.org/CVERecord?id=CVE-2026-102278) | `brace-expansion` | `>= 4.0.0, < 5.0.11` | 5.0.11 | 5.0.9 |
| [GHSA-6j4f-fj2g-mc7p](https://github.com/advisories/GHSA-6j4f-fj2g-mc7p) · [CVE-2026-102276](https://www.cve.org/CVERecord?id=CVE-2026-102276) | `brace-expansion` | `>= 4.0.0, < 5.0.10` | **5.0.10** | 5.0.9 |
| [GHSA-rfgv-xxqx-mfg5](https://github.com/advisories/GHSA-rfgv-xxqx-mfg5) · [CVE-2026-19534](https://www.cve.org/CVERecord?id=CVE-2026-19534) | `undici` | `>= 7.0.0, < 7.29.1` | 7.29.1 | 7.29.0 |
| [GHSA-w293-vg96-wgc3](https://github.com/advisories/GHSA-w293-vg96-wgc3) · [CVE-2026-84961](https://www.cve.org/CVERecord?id=CVE-2026-84961) | `undici` | `>= 7.24.1, < 7.29.1` | 7.29.1 | 7.29.0 |
| [GHSA-68fv-2mgg-jv7q](https://github.com/advisories/GHSA-68fv-2mgg-jv7q) · [CVE-2026-93749](https://www.cve.org/CVERecord?id=CVE-2026-93749) | `source-map-js` | `>= 1.0.0, < 1.2.2` | 1.2.2 | 1.2.1 |

The ranges above are the affected ranges carried in the advisory record used for this comparison, not a
re-derivation from the advisory pages. One of them matters for the choice made here: the second
`brace-expansion` advisory is first patched in **5.0.10**, one release below the 5.0.11 the first one
requires, so 5.0.11 is adopted to satisfy both at once.

## Why no fresh `npm audit`

`npm audit` was **not** re-run for this change. The session's auto-approval for manifest egress to
the public registry was denied, so no audit that reaches the network was performed. What is used
instead is the public advisory record above plus local range comparison, and the method was to
download the public npm high-advisory feed without sending repository metadata and then compare the
affected ranges locally against the tree actually installed under `apps/web/node_modules`. It is not
an `npm audit` replay.

Three claims are therefore deliberately not made: that the raw advisory identifiers were re-verified
by a local `npm audit` run, that the whole dependency tree is at zero, and that this audit as a whole
is clean. The claim here is narrower: for these three packages and these five advisories, the
installed versions are the fixed ones.

## What the behaviour script does and does not show

`apps/web/scripts/check-dependency-patches.mjs` keeps version evidence and behaviour evidence apart.

- Version evidence: the copies this script resolves — the root resolution from `apps/web` plus the
  resolved copy of each parent it names — must be at or above the fixed release, and the range each
  installed parent declares must still admit it. This is not a global walk of every nested installed
  copy in the tree; a copy the script does not resolve is not covered by it.
- Behaviour evidence, only reached once every floor is met: an ordinary brace list expanded in this
  process, then ten concatenated brace groups (1024 strings) and one nested group expanded in a child
  process with a timeout; a source-map round trip; a jsdom document with no resource loading; and
  one scripted connector case for the function-valued `BalancedPool` option the undici advisory
  names, with the abort signal in the request's own options and a connector that calls back with an
  error instead of opening a socket. Only the unpatched shape — the one that drops the option —
  would attempt the loopback address, and nothing outside this host is ever addressed.

**None of the five advisories is reproduced by that script, and it does not claim to be.** It shows
that the ordinary expansion, source-map, DOM and pool-option paths this app and its test tooling use
still work on bounded inputs. Passing it is not evidence that any specific exploit is impossible.

## Evidence this document does not carry

The controller ran the script red before installing, then installed the three patches offline with the
local npm cache and ran it green: the version floors and the behaviour smoke above were reported as
passing at that point. The raw output is kept outside this repository, and this document does not
reproduce it.

What was not run here: any registry-reaching command, any `npm audit`, and the repository's own full
test and build gates against this change. The last of those still awaited verification of the
recovery-panel repair when this document was written, so nothing here should be read as those gates
having passed. The lock change and the script that checks it are what this document records.

Related: [contributing](../CONTRIBUTING.md) for the repository's own local environment and required
checks.
