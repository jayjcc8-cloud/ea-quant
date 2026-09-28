# `ops/launchd`

The macOS local Paper runtime operator: two per-user LaunchAgents plus one small POSIX `sh`
command. It makes `ea paper start` autonomous on this Mac without adding a scheduler, a process
manager, a watchdog or a database.

| File | Role |
| --- | --- |
| `ea-runtime` | the operator command and both launchd entrypoints (`run`, `backup`) |
| `run-config.example` | the pinned inputs the operator copies to `~/EA/supervisor/run-config.env` |
| `com.ea.paper.plist.in` | LaunchAgent template for the supervised foreground `ea paper start` |
| `com.ea.paper-backup.plist.in` | LaunchAgent template for the scheduled `ea paper backup` tick |

Neither `.plist.in` file is a live unit: `ea-runtime install` renders them into
`~/Library/LaunchAgents/`.

Full documentation: [`docs/local-paper-runtime.md`](../../docs/local-paper-runtime.md).
