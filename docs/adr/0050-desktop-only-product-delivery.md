# ADR 0050: Desktop-only QUINE Product Delivery

Date: 2026-10-17

## Status

Accepted — Product Owner authorized the direction after architecture review; the implementation
remains an unpublished candidate. This is not release or deployment authorization.

## Decision

QUINE desktop is the sole supported user entry point, release artifact and final product
acceptance target. The current target is Apple Silicon macOS. Withdraw independent browser,
server and mobile product support. Keep the static introduction site and desktop responsiveness
for narrow content areas and text scaling.

This supersedes the independent browser delivery/acceptance scope of
[ADR 0030](0030-local-web-offline-backtest-integration.md) and the server product support scope of
[ADR 0046](0046-production-runtime-profile-v1.md). Their Accepted texts, historical releases and
validation evidence remain unchanged. Their runtime safety contracts remain applicable.

Keep one React/WebView frontend, `apps/web/dist`, `src/ea/web`, FastAPI/Uvicorn and the `web`
dependency group. Desktop starts `ea.desktop`, which creates the embedded app directly; it does
not depend on `ea web serve`. Retain that command for internal development, installed integration
tests and historical compatibility. Remove only the retired guided browser launcher and its
dedicated tests. No frontend copy, directory migration or IPC replacement is authorized.

`scripts/build_distribution.py`, Linux `scripts/production_runtime.py` and `deploy/*` are retained
historical compatibility tools, outside new QUINE product releases. Do not uninstall existing
services or remove Mac Paper operator tools/drills. Keep the production-runtime workflow as
internal compatibility verification with manual dispatch and existing path-triggered checks;
do not change job IDs, safety assertions or external branch protection in this batch.

## Validation and preserved boundaries

Run shared component, business and API regressions once according to affected risk. Browser E2E
is an internal carrier for this shared code. The final installed desktop owns native startup,
quit, workspace/session ownership, WebView navigation, window sizing, focus, keyboard, IME and
text-scaling acceptance; it does not require a duplicate desktop business E2E suite. Browser or
API passes do not prove native behavior. Native narrow-window, full keyboard and Chinese IME
acceptance currently remain unverified.

Preserve loopback/Host/Origin/JSON/path checks, WebView navigation confinement, single writer/core
ownership, simulation stop confirmation, rejected candidate identity, frozen reports and the
separation of AI Apply, candidate acceptance and explicit execution confirmation. This decision
adds no broker connectivity, Live capability, secrets/permissions changes or publication.
