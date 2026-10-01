# Mac-local Paper runtime operator (launchd)

This is the macOS operator layer that lets one accepted local Paper Candidate run for a long
time with Claude and a human out of the normal loop. It is deliberately thin: **launchd owns
process lifetime and the clock, and the existing `ea paper start|stop|status|backup` commands
stay the only authority over trading, money and evidence.** Nothing here is a scheduler, a
process manager, a watchdog framework, a monitoring platform or a database, and nothing here
changes [local Paper](local-paper.md) itself.

Read [ADR 0049](adr/0049-explicit-local-paper-profile.md) and [local Paper](local-paper.md)
first. This document adds only the host lifecycle around that existing capability.

## The one job of this layer

`ea paper start` is a foreground, blocking process. That is exactly what a supervisor needs, so
the layer does not add daemonization, forking or detaching to EA. It adds:

1. a per-user **LaunchAgent** that runs one foreground `ea paper start` and restart policy that
   distinguishes a crash from an intentional stop;
2. a second per-user LaunchAgent that runs the **existing** `ea paper backup` on a schedule;
3. a small operator command, `ea-runtime`, so the normal workflow is
   `ea-runtime start|stop|status` rather than raw `launchctl`.

## Runtime filesystem boundary

Repository source and build artifacts are never durable runtime state. Everything the runtime
owns lives under `~/EA`, and `~/EA/supervisor` holds the operator layer itself:

```
~/EA/
  runtime/      attempt directories, one per supervised launch (--output-root)
  workspace/    the ACCEPTED-Candidate research workspace (--workspace)
  inputs/       copied examples/paper fixtures (--scenario)
  logs/         launchd-captured stdout/stderr for both jobs
  evidence/     host acceptance evidence
  backups/      backup targets (--backup-root)
  supervisor/
    ea-runtime           the installed operator command
    run-config.env       the pinned inputs (see below)
    templates/           the installed copies of the plist templates
    state/
      current-run              absolute path of the newest supervised attempt
      launch-history.jsonl     one line per supervised launch: run id, dir, pid, time
      captured-runs            run ids already captured by `ea paper backup`, one per line
```

## Install

1. Create the accepted Candidate once, as described in [local Paper](local-paper.md), into
   `~/EA/workspace`, with the fixtures copied into `~/EA/inputs`.
2. Copy `ops/launchd/run-config.example` to `~/EA/supervisor/run-config.env` and edit the pins.
   The file is a **checked data file**: every line must be a plain `NAME=literal` assignment.
   A line that is not one is refused before it is read, so a stray shell fragment cannot run.
3. Install and load both LaunchAgents from the repository checkout:

```sh
sh ops/launchd/ea-runtime install --config ~/EA/supervisor/run-config.env
```

`install` copies `ea-runtime`, the templates and the config into `~/EA/supervisor`, renders
`~/Library/LaunchAgents/com.ea.paper.plist` and `com.ea.paper-backup.plist` from the config, and
`launchctl bootstrap`s both into the user's `gui/<uid>` domain. After that, `RunAtLoad` starts
the supervised run and keeps it running across logins.

## Operator surface

```sh
EA=~/EA/supervisor/ea-runtime
$EA start      # re-arm and wait for a live supervised attempt (no-op if one is live)
$EA stop       # cooperative `ea paper stop`, then confirm it stayed stopped
$EA status     # launchd state on stderr, authoritative `ea paper status` on stdout
$EA log        # tail the launchd-captured Paper output
$EA backup     # run one scheduled-style backup tick by hand
$EA reload     # re-render both LaunchAgents from the current config
$EA uninstall  # boot out both jobs and remove both plists
```

`start` never kills a running job, so it cannot produce a second writer on one attempt.
`stop` writes the durable cooperative stop request that the product already owns and waits for a
terminal state and a released writer lease; it never signals an unrelated PID, and it never
liquidation-sells.

### One supervised writer, and only one

The job the plist loads is the single writer for this Candidate, and the runner enforces that
rather than assuming it:

- The rendered plist is the only caller that passes `ea-runtime run --supervised`. A `run`
  **without** that flag is refused while the paper LaunchAgent is loaded: a hand-run would be a
  second, unsupervised Paper process against the same Candidate, with its own attempt directory,
  writer lease and audit journal, and a later `ea-runtime stop` would aim at the attempt the
  agent recorded rather than at the manual process. `ea-runtime uninstall` unloads the agent and
  frees the manual path again. That check fails **closed**: if launchd cannot be reached at all
  (an `ssh` session with no GUI domain, say), the run is refused rather than read as "not loaded",
  because "I cannot tell" must not be the answer that mints a second writer. Use the product CLI
  `ea paper start` for a hand-run.
- `ea-runtime reload` and `ea-runtime install` refuse while the run is live. Both re-render and
  then boot the label out and back in, and `RunAtLoad` starts a fresh attempt; because `start`
  kickstarts the already-loaded definition, accepting a new config is `stop`, then `reload` or
  `install`, then `start`.
- `EVENT_LIMIT` must be empty. `ea paper start` ends a bounded run with a non-zero
  source-exhaustion exit, and `KeepAlive{SuccessfulExit=false}` restarts an unsuccessful exit by
  design, so a bounded supervised profile would restart forever, minting one attempt directory,
  writer lease and audit journal per restart. `install`, `reload`, `start` and `run` refuse a
  non-empty value before anything is written, rendered or armed — `install` validates the config
  you hand it before it copies it over the pinned one. A bounded run belongs on the direct
  `ea paper start --event-limit N` CLI, which no supervisor restarts.

  `stop`, `status`, `log` and `uninstall` deliberately keep working on a config that carries an
  `EVENT_LIMIT`, so a machine pinned to a bounded config can still be recovered.

Upgrading an already-installed host is `ea-runtime install` from the new checkout: it re-copies
the runner, re-renders both plists and reloads both labels. Replacing only
`~/EA/supervisor/ea-runtime` by hand does **not** re-render the plist, and an old plist's argv
would then be refused by the new runner on every launch.

## Why this distinguishes a crash from an intentional stop

`com.ea.paper.plist` uses the launchd-native rule, and nothing else:

```xml
<key>KeepAlive</key>
<dict><key>SuccessfulExit</key><false/></dict>
```

launchd treats a **successful exit** as exit status 0; a non-zero status or death by signal is
an **unsuccessful exit**. So:

| Event | Exit | launchd |
| --- | --- | --- |
| `ea paper stop` (cooperative stop file) | 0, terminal state `stopped` | stays loaded, stays **stopped** |
| `SIGTERM` to the supervised process | 0, the handler sets `stopping` and winds down | stays loaded, stays **stopped** |
| `SIGKILL`, crash, or admission failure | signal / non-zero | **started again** |

There is no unconditional `KeepAlive`, no wrapper retry loop and no health-check daemon. A
restart is exactly "the last exit was unsuccessful", which is launchd's own definition.

`ThrottleInterval` (default 10s) is launchd's bound on how often the job may be started again.
It bounds a repeated-startup-failure loop rather than hiding it; a configuration that cannot
start is visible as a throttled relaunch loop in `~/EA/logs/paper.err.log` and in
`launchctl print`.

SIGTERM is deliberately **not** the crash drill: this application handles SIGTERM cooperatively
and exits 0, which is a clean stop by design. Only SIGKILL (or a non-zero exit) represents
abnormal termination here.

### A replacement is always a fresh attempt

`ea paper start` refuses to reuse an existing attempt, and `local Paper` never resumes a crashed
attempt into continued trading. The supervised runner therefore mints **one fresh run id per
launch** and records it in `~/EA/supervisor/state/`. A crash replacement is a new attempt with a
new run id, a new writer lease and no resend of anything: the killed attempt stays exactly as
the product left it and is replayable only through the existing M1 `ea paper resume` path,
which decides whether reconciliation is required.

The runner `exec`s `ea paper start` rather than backgrounding it, so launchd's recorded PID *is*
the Paper PID. An abnormal kill of the supervised process can never leave an orphaned trading
process behind.

### Re-arming after a clean stop

After a clean stop the job stays loaded with `state = not running`. `ea-runtime start` calls
`launchctl kickstart`, which starts a job that is not running and does nothing when it already
is. No arming file or extra supervisor state was needed: `SuccessfulExit = false` plus
`kickstart` already expresses "stay stopped when stopped on purpose, start when told".

One consequence worth knowing: `RunAtLoad` means a **login or reboot starts the run again**,
because the plist is loaded fresh. That is the intended autonomy property. If you want a machine
to stay stopped across a reboot, `launchctl disable gui/$(id -u)/com.ea.paper` before rebooting,
and `enable` it again before `ea-runtime start`.

## Scheduled backup

`com.ea.paper-backup.plist` is a second LaunchAgent with `StartInterval` (default
`BACKUP_INTERVAL_SECONDS=300`) that runs `ea-runtime backup`, which invokes the existing
`ea paper backup` once per tick. There is no cron, no APScheduler and no new scheduling code.

`ea paper backup` is not an online snapshot: it refuses a held writer lease and captures only a
settled attempt. A tick therefore captures **the newest attempt whose writer has released and
which has not been captured yet**, and prints `no settled attempt needs capture` otherwise.
Re-running a tick never captures the same attempt twice, and a refused or skipped tick publishes
nothing.

"Has been captured" is recorded in `~/EA/supervisor/state/captured-runs`, one run id per line,
appended only after `ea paper backup` exits 0. It has to be recorded separately from the backup
directory itself: retention prunes older backups, and while the directory was the only evidence
of a capture, pruning it made the attempt look uncaptured and eligible, so the next tick
published a second backup of an attempt that had already been captured. The marker holds no
economic, recovery, reconciliation or runtime authority — nothing reconciles, replays or decides
from it, and no other command reads it. A refused or failed capture writes nothing, so that
attempt stays eligible; an attempt captured before this file existed still reads as captured
through its backup directory.

### Making an attempt capture-eligible again

`captured-runs` is deliberately independent of `BACKUP_ROOT`. Retention only prunes inside the
backup root it is given, so normal retention can never remove a marker entry — that is exactly why
the file exists, and it is also why moving or replacing the backup root does not re-capture the
attempts already recorded in it.

The one exceptional case where a run must become eligible again — the backup root was intentionally
moved or replaced, or a capture was found corrupt and deleted — is recovered by hand:

1. Confirm the run really is uncaptured in the root you now use, for example with
   `ea paper backup inspect --backup DIR`.
2. Delete that run's single line from `~/EA/supervisor/state/captured-runs`.
3. Let the next `ea-runtime backup` tick capture it into the current root as usual.

Deleting the line is an exceptional operator recovery action, not cleanup, and it must never be
part of routine retention or gate preparation. It restores nothing, replays nothing, reconciles
nothing and authorizes nothing — it only makes that one settled attempt eligible for the existing
capture path again, and `ea paper backup` still applies every one of its own refusals.

## Gate preparation

The M4 release-candidate gates (72h, 7d) are judged by the frozen WU-2 contract:
`ea.product.paper_evidence.build_paper_snapshot` produces one `PaperSnapshot`,
`build_gate_identity` one `PaperGateIdentity`, and the pure `evaluate_gate` the verdict. This
layer adds only the thin operational surface those need on a real host — it adds no second
evaluator, no timer, no watchdog and no gate state.

```
ea-runtime snapshot --repo CHECKOUT [--alert-stream PATH]
ea paper gate lock     --identity FILE --gate-id ID --gate-type 72h|7d \
                       --repo CHECKOUT --config run-config.env --launchd-dir DIR
ea paper gate evaluate --identity FILE --run-dir DIR --repo CHECKOUT \
                       --config run-config.env --launchd-dir DIR \
                       [--supervisor-state running|not_running|unknown] \
                       [--backup-root ROOT] [--alert-stream PATH]
```

`ea-runtime snapshot` is the real-host path: it reads launchd's own answer for the Paper job and
the attempt the agent recorded, takes the backup root from the pinned config, and passes those
plus the operator's `--repo` and `--alert-stream` to `ea paper snapshot`. `--repo` and
`--alert-stream` stay explicit arguments because neither is derivable from the pinned config:
`WORKSPACE` is the installed-Candidate workspace rather than this checkout, and the host alert
stream has no configured location.

Every snapshot input is another owner's existing read, and each is written down where it comes
from:

| Snapshot field | Read from | Unobservable becomes |
| --- | --- | --- |
| `repo_sha` | `git -C CHECKOUT rev-parse HEAD` | the command fails, no snapshot |
| `run_id`, `health`, `runtime_state`, `reconciliation_state`, `risk_gate_state`, `readiness` | `ea paper status` (the health projection, with liveness re-observed from the live writer lease) | an absent or unavailable projection is `health = null`, `unknown`, not ready |
| `writer_lease_held` | the live `flock` observation of `writer-v1.lock` | the command fails, no snapshot |
| `supervisor_state` | launchd, or the operator's explicit `--supervisor-state` | `unknown` |
| `alert_state` | the durable alert stream | `unavailable`, never `clean` |
| `backup_state` | `latest_verified_paper_backup` | `absent` when none verifies, `unavailable` when the root cannot be read |
| `broker_mode`, `live_enabled` | the attempt's own recorded profile | anything that is not the local simulated Paper profile reads as live-capable |

The snapshot is read-only and side-effect free, and it recomputes no economic, risk,
reconciliation or readiness truth: an input it cannot observe keeps the contract's own
unavailable value rather than becoming healthy evidence, so a gate prepared against a blind
observer cannot come out `PASS`. `ea paper snapshot` exits 3 when the snapshot itself cannot be
taken.

`ea paper gate lock` rebuilds the identity from the real inputs — the checkout's HEAD, the
operator config's bytes and the two rendered LaunchAgent definitions — and persists it once. It
is one small JSON document at the path you give it, and its write is an exclusive create: an
identity that is already locked is never replaced, because drift must become `INVALID`, not a
fresh gate. Editing the pinned config or re-rendering a plist afterwards therefore fails the
gate instead of silently relocking it.

**Locking an identity does not start a gate.** This product owns no clock, no timer and no gate
state, so nothing begins when that file is written; `evaluate_gate` is the only verdict
authority and `ea paper gate evaluate` is only a reading of the lock against the runtime as it is
now. The 72-hour and 7-day Gates are started by the M4 release-candidate freeze, a separate
authorization, and exit code 3 means the verdict is not `PASS` or the observation could not be
taken.

Before that freeze, the minimum a host must show is: the checkout HEAD is observable and is the
commit being frozen; the pinned config and both rendered LaunchAgents are observable and will not
move; exactly one supervised writer is intact (`ea-runtime status`, one live writer lease); the
snapshot command runs; the backup root and the alert stream are readable; and Live remains
denied, which the snapshot reports when the attempt's own profile is anything but local
simulated Paper.

## Boundaries

- **Live is denied.** This layer never sets a run mode, never touches credentials and never
  submits an order. It runs `ea paper start` with the command's own local inputs.
- The layer owns no trading decision, no risk limit, no kill switch and no reconciliation.
  `ea paper status` remains the authoritative operator surface; `launchctl` state is diagnostics.
- The layer is macOS-specific and per-user. The Linux Web research service keeps its own
  systemd unit in [`deploy/ea.service`](../deploy/ea.service); the two are unrelated.
- Nothing here changes power management. Sleep can still stop the machine; see
  [Host continuity](#host-continuity).

## Host continuity

`scripts/accept_local_paper_runtime.py` records, but does not change, the facts that decide
whether a host can stay up for a long run: AC/battery state (`pmset -g batt`), the sleep
settings (`pmset -g custom`) and free disk (`df -h ~`). If a soak is wanted, keep the Mac on AC
power and check that system sleep will not suspend it; the acceptance reports the exact current
values rather than silently hardening the machine.

## Host acceptance

```sh
uv run --no-project --python 3.12 python scripts/accept_local_paper_runtime.py
```

This is the macOS counterpart of
[`scripts/accept_production_runtime.py`](../scripts/accept_production_runtime.py). It prepares or
reuses an accepted Candidate under `~/EA/workspace`, installs the real LaunchAgents, runs four
host drills, writes machine-readable evidence under `~/EA/evidence/wu1-<stamp>/`, and uninstalls
the LaunchAgents again unless `--keep-installed` is given.

| Drill | What it proves |
| --- | --- |
| A. No Claude | the supervised run has no Claude process in its ancestry, and reports `runtime_ready` with the expected reconciliation state |
| B. Unexpected restart | SIGKILL makes launchd replace the process; the replacement is a fresh run id with a single writer, and the killed attempt is still classified only by `ea paper resume` |
| C. Scheduled backup | the backup LaunchAgent fires on its own and `ea paper backup inspect` verifies the capture |
| D. Stop/start | a cooperative stop stays stopped across an observation interval longer than the restart throttle, and a later `start` brings a supervised run back |

## Limits this layer does not remove

The layer proves the Mac lifecycle, not long-duration stability. It does not establish a 72-hour
soak, real-provider semantics, crash recovery against an external broker, VPS operation, or Live
safety. Those remain governed by the phase they belong to.
