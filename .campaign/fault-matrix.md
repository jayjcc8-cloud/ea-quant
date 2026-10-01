# PPV-16 Integrated Fault Campaign — Fault Matrix (M4 fault phase only)

Authoritative start main: `4a012ea784788ce9371f8dd6b47fdeb7458428ad` (WU-3 merged).
LIVE: DENIED. No 72h/7d soak, no Gate Prep, no #243 item 5. One representative
integrated proof per materially different failure semantic.

Legend — seam: `host` = real process / launchd path on this Mac; `proc` = real
`ea paper` subprocess; `det` = deterministic in-process seam through the real
`run_local_paper` composition and real POSIX journal.

| # | Fault injected | Boundary | Precondition | Expected deterministic response | Authoritative evidence | Forbidden outcome | Recovery criterion | Seam |
|---|---|---|---|---|---|---|---|---|
| F-01 | Feed stall/disconnect (SIGSTOP > stall window, then SIGCONT) | `StreamingMarketRuntime.heartbeat` stall guard + `poll` freshness guard inside `run_local_market_stream` | live `ea paper start` attempt, ≥2 durable fills | on resume the gap is detected (`source_stalled` or `stale_market` drain); new decisions cease; already-issued facts drain within bound; no fabricated market event; non-zero exit | final status reason + frozen `market_events`/`fact_events`/`pending_facts` vs pre-stall snapshot; `ea paper resume` classification | keep trading on a silent feed; invent events to stay "healthy"; exit 0 | launchd replacement is a fresh attempt; resume classifies the stalled attempt truthfully | host |
| F-02 | Feed exhaustion (`--event-limit`) | `LocalSimulatedMarketSource` limit + `run_local_market_stream` exhaustion | CLI `--event-limit 3` | stops new decisions, drains issued facts, exits 3 with `source_exhausted`, no fabrication | result JSON + status counts + resume | fabricated events; zero exit | attempt is terminal (RUN_TERMINAL durable); supervisor refuses EVENT_LIMIT by design (WU-4) | proc |
| F-03 | Duplicate execution fact | broker query redelivery → fact-authority dedup → ledger handoff, on the real CLI path | full 3-round-trip run to cooperative stop | duplicate delivery is economically exactly-once: `duplicate_facts`=6, `observed_fills`=6, `fills`=6, `ledger_sequence`=7, cash 986.8, reconciliation `match` | status + start-result JSON + resume | second ledger effect | n/a | proc |
| F-04 | Late fact after terminal command | same query-after-fill redelivery; the redelivered trade fact is processed after the order is terminal | same run | the late fact is processed as DUPLICATE and retained as evidence, never silently discarded, no new effect | `duplicate_facts`, `EXECUTION_FACT_PROCESSING_OUTCOME` duplicate records, resume `terminal=filled` per order once | silent discard of an authoritative fact | n/a | proc |
| F-05 | Uncertain submit, deterministic windows | `crash_after` seam after `PAPER_SUBMISSION_AUTHORIZATION`, `PAPER_SUBMISSION_RESULT`, `PAPER_FACT_DISPATCH`, `EXECUTION_FACT_PROCESSING_OUTCOME`, `PORTFOLIO_LEDGER_HANDOFF_OUTCOME` | fresh attempt per window | authz-only → UNKNOWN, broker query resolves not_sent, `reconciliation_required`=false, never resent; result-only → SENT_CONFIRMED open kept for continuation; fact-dispatched → terminal filled, exactly once; processing/ledger windows → filled, exactly once | resume JSON per window + journal record sets | blind resend; guessing away ambiguity; duplicate economic effect | resume admission is the sole continuation authority | det |
| F-06 | Uncertain submit, real SIGKILL storm | real `ea paper start` process killed at ~12 random offsets mid-run | fresh attempt per kill | every killed attempt's resume admission matches its durable records (independent verifier); never a resend; exactly one fill per filled intent; no divergence | resume JSON + independent journal-truth verifier per kill | wrong classification; auto-continued trading; duplicate fill | resume admission; killed attempt never auto-resumed | proc |
| F-07 | Durable pre-effect write failure | `PosixAuditJournal` with injected `_AuditOps` failure at the `PAPER_SUBMISSION_AUTHORIZATION` frame (pwrite variant and post-write fsync variant) | fresh attempt per variant | append fails → run fails closed: no broker submit, no economic side effect, journal monotone failed/needs-rescan, status failed+incomplete, `terminal_durable`=false; resume never invents authority | run result + journal state + resume JSON | economic side effect without durable pre-effect authorization; false success verdict | resume: pwrite variant → no intent (truthful); fsync variant → UNKNOWN intent, broker resolves not_sent | det |
| F-08 | Durable write torn tail | journal reopen `_rescan(permit_torn_tail=True)` after a mid-append kill + deterministic partial-frame append | killed/crashed attempt | torn frame truncated durably; classification equals the pre-torn record set; chain stays consistent | resume JSON after torn-tail repair | corrupted journal accepted or invented authority | resume truthful | proc/det |
| F-09 | Backup + restore after fault | `ea paper backup` / `inspect` / `restore` over a crashed (lease-released) attempt; `ea paper resume` on the restored NEW attempt | crashed attempt from F-06 + baseline stopped attempt | capture only after lease release; inspect verifies; restore materialises into a NEW dir; resume classification identical to the source attempt; source untouched; no duplicate economic effects | backup manifest + inspect + resume JSONs (source vs restored) | mutate the failed attempt in place; refuse a settled crashed attempt; divergent restored classification | restore + resume path | proc |
| F-10 | Process crash under launchd supervision | isolated `EA_RUNTIME_HOME`/`EA_LAUNCH_AGENTS_DIR` install; SIGKILL of the supervised PID | live supervised attempt | abnormal death is observable (signal, last exit non-zero); launchd restarts (KeepAlive SuccessfulExit=false); replacement is a fresh run id; exactly one writer at all times; killed attempt classified only by resume; intentional stop stays stopped (exit 0) past ThrottleInterval | launch-history.jsonl, `launchctl print`, current-run, status/resume of both attempts, process table | second writer; killed attempt auto-continued; stop mistaken for crash | launchd replacement; `ea-runtime stop` for intentional stop | host |

F-10 seam note: a launchd-spawned job does not inherit `EA_RUNTIME_HOME` from
the launching shell, and without it `ea-runtime` falls back to `$HOME/EA` — the
real operator state — so the job environment must pin the rig paths before the
first spawn. The drill proves the fail-closed refusal at shell level (missing
HOME → deterministic refusal, zero state written), installs with
`EA_RUNTIME_NO_BOOTSTRAP=1` (render only, no job spawn), then injects HOME +
`EA_RUNTIME_HOME` + `EA_LAUNCH_AGENTS_DIR` into the drill's own rendered plists
and bootstraps. Additionally, launchd-spawned jobs in this session carry no TCC
grant for `~/Documents` (com.apple.macl on the checkout tree), so the rig — a
copied venv with a non-editable `ea` install (same package bytes, same acceptance
code digest), fixtures and runtime home — lives under `/tmp`. The drill asserts
the real operator `~/EA/supervisor/state` is byte-identical before and after.
Repository templates and the supervisor script are untouched.

## Cross-cutting invariants

- **Gate invariant (WU-2):** no drill, observer run or diagnostic touches gate identity or
  verdicts. Asserted: `git diff` on `src/ea/product/paper_evidence.py` empty before/after;
  no drill writes under `src/`; deterministic verdicts never re-judged by tooling.
- **WU-3 observer:** run once (baseline attempt) for diagnosis; availability/opinion must not
  affect any outcome; asserted to write nothing into the attempt.
- **NO_DUPLICATE_ECONOMIC_EFFECT / FAIL_CLOSED / EVIDENCE_TRUTHFUL** are mandatory PASS for
  every applicable case; each drill asserts its own evidence of these.
- Drill tooling mutates only campaign-owned state under the worktree `.campaign/` root and
  the isolated launchd home; the WU-1 `~/EA` operator state is read-only input (fixtures).

## Not in scope (deferred, reported only)

- #243 item 5 retention re-capture → WU-5 Gate Prep.
- Gate Prep / RC freeze / 72h / 7d soak / PPV-17 / real broker — not authorized in this unit.
