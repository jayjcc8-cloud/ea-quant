# Research First Release — Implemented vs. Pending

This note separates what the project has implemented and tested (inherited evidence, §1) from what
still gates a public release of this candidate (§2). It is a planning note under
[STATUS](STATUS.md); it is not a release announcement, not a version decision, and not a new
architecture contract. Where this note and the merged code disagree, the merged code and its tests
are authoritative.

No publication, tag, registry upload, or deployment has been performed.

## 1. Implemented and tested (inherited evidence)

These capabilities exist and are exercised by the repository's own tests and CLI, outside the
developer's checkout where noted. Each is recorded with its durable-state entry in
[STATUS](STATUS.md). This section is **inherited STATUS evidence**: it records what the project has
already established, not a fresh re-verification of every capability on this candidate. The
verifications run specifically for this candidate are the isolated UI verification and the formal
installed Web E2E, both recorded in §2.

### Deterministic offline engine

- Strict `BacktestScenario` validation that performs no economic mutation (`ea backtest validate`).
- Exactly-once, currency-bound, replay-safe initial funding; conflicting funding fails closed.
- Bounded risk limits (max order quantity, max position, available cash, max notional) enforced
  before trading mutation.
- Deterministic execution costs: versioned commission policy, adverse slippage in basis points, and
  explicit execution latency, each frozen into the run's identity.
- Attempt identity and reproducibility lineage: fresh processes get distinct RunIds with an equal
  semantic/economic outcome projection.
- Supported resume across three bounded durable frontiers (`ea backtest resume`), with failure,
  conflict, corruption, and partial-publication paths failing closed or idempotent.
- Formal, byte-stable reports over verified completed attempts (`ea backtest report`), plus
  deterministic equity-path evidence and maximum drawdown, and read-only trade analytics over
  verified reports.
- Installed-wheel execution outside a Git checkout for the fixed offline slice.

### Local Web research loop

- `ea web serve` fixes the listener to `127.0.0.1`, serves the production React build and the
  same-origin API, and keeps scenario, workspace, strategy/data, and UI roots separate.
- Host/origin/header/content-type, identifier, artifact-allowlist, and resolved-root checks fail
  closed at the HTTP boundary; no CORS relaxation, arbitrary browser path, or CDN. Data entry is a
  controlled HTTP upload: the file passes the same strict Phase-1 OHLCV decoder, and a published
  upload becomes one more entry in the same dataset catalog. Arbitrary paths and unrestricted file
  writes are not part of the product.
- Single-active-job workspace with an atomic job index separate from attempt evidence; interrupted
  jobs are preserved without automatic rerun.
- Research tasks: durable idea, spec proposal constrained to the registered catalog, spec
  versioning on confirm, one real backtest per attempt, and status re-derived from real jobs on
  every read.
- Bounded experiment batches (2–10 members), pairwise comparison, chronological holdout, and
  path-aware analysis over persisted reports — all explicitly human-selected, with no ranking or
  recommendation.
- One bounded evidence conclusion per succeeded run: at most one model call, read from the verified
  report, against a criterion frozen before the run. It starts no new backtest and never changes the
  result.
- The UI keeps the result first: a published conclusion is shown with the verified run behind it,
  never instead of it.
- Backtest status and conclusion status are **separate axes**. A task may hold a published
  conclusion, a recorded interpretation failure, or a conclusion closed by the user's cancellation;
  a succeeded run without a conclusion reads “conclusion not published” — an empty slot, never an
  invented conclusion. Once the conclusion axis is closed it is never reopened, so no new model-call
  slot may be reserved for it and no late result or failure may overwrite it.
- Pending and unproven boundaries stay neutral. A submission that left the process with an unproven
  outcome keeps its charge and is never silently retried or marked failed; the only new-attempt path
  offered is an explicit cancel plus a new task. Retry is offered only when the failure provably
  spent nothing or reached a terminal provider answer. There is no fake retry.
- Cancellation is not a rollback. It stops future orchestration, does not kill or undo a backtest
  that already started, and leaves a completed run and its verified report exactly as they are.

### Local strategy and data inputs

- Immutable local `.eastrategy` artifacts through `ea strategy pack|validate|inspect`, with
  Strategy SDK V1/V2 and package V1/V2/V3, and Scenario V1–V5 identity domains that leave old
  identities unchanged.
- Registered local OHLCV through explicit `--data-root`, with strict discovery and canonical
  inspection (`ea data inspect`).
- Research candidates: immutable evidence fingerprints with human ACCEPTED/REJECTED reasons that
  grant no execution permission.

### Simulated Paper (separate runtime, available)

- Mac-local simulated Paper under `launchd`: start/status/stop, one writer, durable evidence, and
  explicit cooperative stop.
- M1 crash-safe Paper (recovery, broker reconciliation, kill switch, resume), M2 production risk
  guards (ten non-bypassable guards producing one ALLOW/DENY/HALT), and M3 self-operating Paper
  (health/readiness, backup/restore, monitoring/alerts) are recorded as SATISFIED.
- M4 fault campaign (#254) is SATISFIED over the existing runtime boundaries with zero product-code
  repairs.
- Paper Gate prep surface: `ea-runtime snapshot`, `ea paper snapshot`, and
  `ea paper gate lock|evaluate`.

## 2. Pending publish gates (not satisfied)

None of the following gates is satisfied. Each is a prerequisite, not a formality.

- **Paper soak.** The 72-hour and 7-day Paper soak has no recorded PASS. The remaining M4 steps are
  the release-candidate freeze and the soak. This is the P1 prerequisite for any Paper-maturity
  claim.
- **Full exact-head release verification.** Two verifications are recorded. The **isolated UI
  verification** was completed against a copied, safe historical report: no new provider calls, no
  new backtests, no overflow at desktop or mobile widths, and a stable state across GET refresh. The
  **formal installed Web E2E** run passed **8** checks, covering a scripted model write, confirm, a
  real engine run, report, conclusion, refresh, restart, and cancel — with **no real provider key**.
  Full exact-head release verification is **required before publication**: interim formatter and type
  blockers were repaired, and the final result is recorded with the candidate SHA. The candidate has
  not been re-verified capability-by-capability.
- **Version and tag decision.** The public package version remains `0.2.0` because the owner has not
  decided otherwise (see §3).
- **Release announcement.** The final, version-specific release announcement is pending the owner's
  version decision; no announcement has been written or published.
- **No remote publication.** This work explicitly performs no remote publication, no tag, no
  package-registry upload, and no deployment.

## 3. Version and tag proposal (proposal only)

- The existing public package version is `0.2.0`, published as a GitHub **prerelease**. That
  prerelease predates this candidate and does not contain the research loop described above.
- The proposed next version is **`0.3.0rc1`** with tag **`v0.3.0-rc.1`**. This is a proposal for the
  owner to accept, change, or reject. No version string, tag, release, or registry entry has been
  edited or created.
- Until the owner decides, documentation and the UI must not describe `0.2.0` or `v0.3.0-rc.1` as a
  completed release of these capabilities.

## 4. Future paths (not delivered)

These are named only to keep the boundary honest. They are not implemented, not scheduled by this
note, and each would need its own bounded Issue and acceptance criteria.

- **External Paper provider (realtime).** Broker/vendor connectivity for Paper is not implemented.
  Mac-local Paper is simulated with an in-process feed and broker; it does not reach an external
  account.
- **Live trading.** Denied and unavailable. Any Live capability would require explicit Human
  Product Owner authorization and a separate release decision.
- **Broad-market data.** Registered local OHLCV is bounded to the files you supply; there is no
  market-data subscription, symbol search, all-market universe, or data download.
- **Realtime / streaming claims.** No verified real-time, all-market, robustness, or profit claim is
  made anywhere in the product or its documentation.
- **Safe automatic strategy generation.** RADIAN does not claim safe automatic strategy engineering.
  The strategy-generation step (RAD-03) is not delivered, and no isolation boundary or coverage is
  recorded yet.
- **Autonomous optimization or trading.** Continuous research means repeated independent,
  explicitly confirmed experiments by a user — never autonomous background optimization or trading.
- **Host deployment.** VPS deployment and reboot acceptance remain not host-verified, and host
  verification would cover host attributes only, never product logic.

## 5. Entry points and contributor setup

CLI and documentation entry points:

- Backtest CLI: `ea backtest validate|run|report|resume`, `ea data inspect` —
  [local-strategy-package-v1.md](local-strategy-package-v1.md)
- Strategy packages: `ea strategy pack|validate|inspect` —
  [local-strategy-package-v2.md](local-strategy-package-v2.md)
- Candidate lifecycle: [candidate-lifecycle.md](candidate-lifecycle.md)
- AI-native local product surface: [radian-local-product.md](radian-local-product.md)
- Web app details and supported checks: [apps/web/README.md](../apps/web/README.md)
- Static project page: [site/index.html](site/index.html)
- Durable state and delivery rules: [STATUS](STATUS.md), [WORKFLOW](governance/WORKFLOW.md),
  [CONTRIBUTING](../CONTRIBUTING.md)

Local contributor setup and required checks:

```bash
python3 scripts/bootstrap_local.py
venv/bin/ea doctor
uv run --no-project --python 3.12 python scripts/verify.py --profile quality
uv run --no-project --python 3.12 python scripts/verify.py --profile full
```

## 6. How this note was verified

- **Inherited evidence (§1).** Statements about implemented behavior are grounded in the merged
  code, the repository tests and CI, and the durable-state entries in [STATUS](STATUS.md). They are
  inherited project evidence; they are not a fresh re-verification of every capability on this
  candidate.
- **Candidate-specific verification (§2).** The isolated UI verification used a copied, safe
  historical report: no new provider calls, no new backtests, no overflow at desktop or mobile
  widths, and a stable state across GET refresh. The formal installed Web E2E run passed 8 checks
  with no real provider key. Full exact-head release verification is required before publication and
  is recorded with the candidate SHA.
- This note adds no runtime, gate, schema, version, or architecture change; it is documentation
  only.
- A claim is absent from this note unless the supplied facts support it. Where a capability is
  partial or conditional (for example, model-backed steps require a configured provider), that
  condition is stated with the claim.
