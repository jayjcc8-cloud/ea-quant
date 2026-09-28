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
which has no backup yet**, and prints `no settled attempt needs capture` otherwise. Re-running a
tick never captures the same attempt twice, and a refused or skipped tick publishes nothing.

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
