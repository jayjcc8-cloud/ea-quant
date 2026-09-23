# Production Runtime Profile V1 (PPV-02)

This profile runs the existing **offline research Web service**, not a trading daemon. Linux
with systemd is the canonical single-server target. It adds no feed, broker, Paper/Live,
readiness, monitoring, backups or runtime recovery semantics. Deployment to a real server
requires separate operator authorization. Loopback remains mandatory; no proxy, TLS or public
listener is configured. Use an operator-controlled SSH tunnel if remote browser access is needed.

## Reused capability and gaps

| Capability at PPV-01 | Inspection | PPV-02 disposition |
| --- | --- | --- |
| Commit-bound wheel/UI bundle and hash-locked dependencies | SATISFIED | Reuse `build_distribution.py` and manifest |
| Installed CLI/Web, loopback, workspace single writer | SATISFIED | Exec unchanged `ea web serve` |
| Job/attempt/report persistence | SATISFIED | Keep one workspace outside releases |
| Fixed production configuration and process manager | MISSING | Strict TOML launcher and systemd unit |
| Boot startup and bounded failure restart | MISSING | systemd enablement and start limits |
| Pinned activation and compatible rollback procedure | MISSING | Stop, validate, explicitly select, start |
| Host deployment, recovery, Paper/Live | DEFERRED | No claim or activation |

## Canonical layout and configuration

Use one dedicated unprivileged `ea` account and this layout. Administrator-owned releases,
launcher, unit, configuration and inputs are readable but not writable by `ea`. Only the workspace
is writable by the service. Provision these permissions explicitly; the repository does not
create accounts or elevate privileges.

```text
/srv/ea/
  runtime.py                  # launcher from the verified PPV-02 bundle; retained across switches
  runtime.toml                # explicit configuration, operator owned
  current -> releases/<full-commit>
  releases/<full-commit>/      # extracted verified existing bundle; immutable after installation
    manifest.json, SHA256SUMS, *.whl, requirements.txt, ui/, .venv/
  inputs/scenarios/           # persistent authorized scenarios and their relative local CSVs
  inputs/data/                # optional existing data-root
  inputs/strategies/          # optional trusted local executable strategy-root
  workspace/                 # existing Web jobs, attempts, reports, batches, holdouts and lock
```

Do not put input directories or workspace under `current` or a release. Do not copy/move workspace
on upgrade. Keep input roots distinct; their contents remain subject to existing validation and
frozen-input semantics. Symlink-resolved roots cannot overlap. Existing workspaces can be used by
setting their absolute location and adjusting the unit's `ReadWritePaths` to that same location.
The existing audit journal requires at least 13 GiB free on the workspace filesystem at admission;
provision additional room for durable results. This profile does not lower that requirement.
There is one service instance and one writer; do not run another CLI/Web writer on this workspace.

Copy `runtime.toml.example` and replace all identity placeholders. The configuration requires
`bundle_root`, full `commit`, SHA-256 of `manifest.json`, `scenario_root`, `workspace`, and integer
unprivileged `port`; optional `data_root`/`strategy_root` map directly to existing Web CLI flags.
No environment fallback, host override, trading mode or secrets field is accepted. There is no
new persistence format. The launcher validates the pinned manifest and payloads, interpreter
location and installed EA package bytes against the wheel before executing the existing CLI.
`-I` removes source-directory/PYTHONPATH injection. Local administrator integrity remains a trust
boundary; hashes are integrity/identity checks, not publisher signatures or a sandbox for strategies.

## Prepare a pinned release (operator procedure)

1. Obtain an explicitly selected bundle and independently record its archive SHA-256 and full
   source commit. Build an unpublished bundle using the existing distribution instructions if
   needed. Extract it once into `/srv/ea/releases/<full-commit>`; never overwrite a release.
2. Run `sha256sum -c SHA256SUMS` in that directory. Check `manifest.json` commit/version against the
   selected artifact. Record `sha256sum manifest.json` in the configuration. Do not trust a hash
   copied from an unrelated download or mutable checkout.
3. Using system Python 3.12 accessible to systemd (outside `/home` and `/tmp`), install exactly as
   in `INSTALL.md`: create `.venv`, install `requirements.txt` with `--require-hashes`, then the
   one local wheel with `--no-deps`. Dependency installation may need package-index access;
   service operation is local/offline. Do not use an editable install or relocate a created venv.
4. On initial setup, copy the verified PPV-02 `runtime.py` to `/srv/ea/runtime.py`, and copy authorized
   scenarios/CSV inputs into `/srv/ea/inputs/scenarios`. Create the writable empty workspace, or
   point at the established one. Keep the launcher pinned and operator owned across application
   switches; changing the launcher is a separate explicit profile update.
5. Write configuration for the exact release and run its check command. This is read-only:

```sh
/srv/ea/releases/<full-commit>/.venv/bin/python -I /srv/ea/runtime.py check --config /srv/ea/runtime.toml
```

The JSON identity includes commit, version, manifest/wheel hashes, installed Python prefix,
workspace and loopback address. `ea --version` alone is insufficient because multiple commits
share distribution version 0.2.0. Wrong configuration/artifact/installation fails with exit 78.

## Supervision and first activation

After operator authorization, point `current` to the selected release. Install the bundled
`ea.service` as `/etc/systemd/system/ea.service` and inspect it with `systemd-analyze verify`.
The service account, inputs and workspace must already exist. Configure a different installation
prefix/user only by explicitly substituting the unit paths/user and matching TOML paths.

```sh
sudo systemctl daemon-reload
sudo systemctl enable ea.service
sudo systemctl start ea.service
sudo systemctl status ea.service
sudo systemctl is-enabled ea.service
sudo systemctl show ea.service -p MainPID -p NRestarts -p Result
sudo journalctl -u ea.service -b
sudo systemctl restart ea.service
sudo systemctl stop ea.service
```

`multi-user.target` enablement makes the service eligible for boot startup; only an actual reboot
on the deployment host proves that host's boot behavior. `Restart=on-failure`, a 5-second delay,
and at most 3 starts in a 60-second window bound a rapid failure loop. Exit 78 is not restarted.
An operator stop does not restart. After correcting a start-limit failure, explicitly run
`systemctl reset-failed ea.service` and start again. A clean application exit is not auto-restarted.
SIGTERM gets 60 seconds before systemd kills the service control group. Existing interrupted-job
semantics apply after interruption: no automatic job retry, economic resume or state repair.

The launcher prints pinned identity before exec, and its PID becomes the one Web process.
Inspect the latest startup JSON in the journal with the current `MainPID`; inspecting a changed
configuration alone does not identify a previously running process. Bind remains `127.0.0.1`.
Logs use the supervisor's standard stdout/stderr capture; PPV-04 logging work is deferred.

## Activation and rollback

Retain the previous release, previous TOML and launcher identity. Prepare/check the new release
before stopping the existing service, using a separate operator-owned TOML candidate. The
workspace must remain the same. Do not switch while a job is active: complete it or explicitly
accept existing interrupted-job semantics. Stop the unit and confirm `is-active` reports inactive
and `MainPID=0` before changing selection; do not bypass an occupied workspace lock.

While stopped, atomically replace `runtime.toml` with the validated candidate, then replace
`current` using a temporary symlink and `os.replace` (or equivalent rename). Because the service
is stopped, these two updates need not be simultaneous; if interrupted midway, interpreter versus
bundle-root/commit checks fail closed at next startup. Start the unit, inspect the startup identity,
and reopen a known completed report. Neither selection change copies or rewrites workspace data.

Rollback is **application rollback, not state downgrade**. Before activation, establish that the
previous runtime can still read the state the new runtime will write, using release-specific
reader/writer evidence and an isolated acceptance run. For PPV-02 versus PPV-01, EA package bytes
are unchanged and the acceptance harness asserts that fact before testing both installed versions
against the same completed job. The launcher is retained during this application rollback.

If activation fails, stop the unit first. Only with established state compatibility, restore the
previous TOML and `current`, check using the previous interpreter, then start and reopen the known
report. If compatibility is unknown, or state was migrated, **leave the service stopped** and
retain all evidence. Do not delete indexes, rewrite reports, rerun jobs, restore a partial snapshot
or blindly point an old reader at new state. Backup/restore and migrations are outside PPV-02.

## Acceptance and limits

`tests/unit/test_production_runtime.py` tests strict configuration, integrity and isolated commands.
`scripts/accept_production_runtime.py` uses two real bundles outside source, noneditable installs,
actual HTTP validation → job → formal report/download, controlled restart, version activation and
compatible rollback. With `--systemd`, it uses an isolated named unit with the canonical policy,
tests start/stop/status/enablement, kills the real service process, verifies a replacement PID and
bounded restart exhaustion, and checks clean stop. It restores/removes only its own test unit.
Linux CI runs this mode. Direct-process mode on macOS verifies the same launch and persistence
path but reports systemd behavior as NOT_YET_HOST_VERIFIED.

Linux CI is an isolated supervisor acceptance host, not the intended VPS. Actual VPS provisioning,
permissions, boot/reboot behavior and deployment acceptance remain NOT_YET_HOST_VERIFIED until
separately authorized and executed. Existing health API use in tests establishes HTTP availability,
not a new readiness contract. Long-running soak, active-trade recovery, Paper/Live and all later
PPV units remain deferred.
