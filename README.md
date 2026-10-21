# QUINE — Local Mac Research Desktop

QUINE is an AI-assisted quantitative research application built on the EA deterministic engine.
The **Mac desktop is the sole supported user entry point, release artifact and final product
acceptance target**. The current Apple Silicon macOS build is an **unpublished local candidate**;
there is no public desktop download or completed release to announce.

## Start with the desktop

Open the explicitly supplied `QUINE.app` candidate normally. An installed app contains its UI,
Python runtime and Rust core; users do not install a Web server, enter a browser address, or need
Node/Python development tools. Do not change macOS security settings to run an untrusted copy.
Signing, notarization and public distribution remain separate, unauthorized release work.

See [Mac desktop setup and boundaries](docs/desktop-macos.md),
[starting research](docs/local-research-start.md), and the
[first-use acceptance card](docs/first-use-acceptance.md). Contributors build an unpublished
candidate using the instructions in the desktop document.

## Current status

| Capability | Status |
| --- | --- |
| Apple Silicon macOS desktop | **LOCAL CANDIDATE — NOT PUBLISHED** |
| Local deterministic research / backtest / formal reports | **AVAILABLE IN THE CANDIDATE** |
| AI collaboration | **REQUIRES AN EXPLICITLY CONFIGURED MODEL**; off by default |
| Local simulated Paper | **SIMULATED ONLY**; explicitly accepted candidate and separate start confirmation |
| External trading platforms / Live | **NOT CONNECTED / PREVIEW ONLY**; no real funds |
| Native narrow-window, full keyboard and Chinese IME acceptance | **NOT YET VERIFIED** |
| 72-hour / 7-day Paper soak | **NO PASS RECORDED** |

## Research with your own evidence

1. Save a research idea in **Research**. Saving an idea needs no model call.
2. Manage shared inputs in **Resources → Data**. Declare the source, licence, timestamp format
   and availability policy, preview the CSV, then explicitly publish and bind its immutable version
   to the intended draft. Uploading does not automatically select it for research.
3. Review the draft's strategy, instrument, sample window, initial funding/currency and assumptions.
   An AI proposal names its target/version, differences and evidence; **Apply changes only the
   draft**. It does not approve or execute an experiment.
4. Preview the experiment and explicitly confirm one local run. Formal results come from the
   deterministic engine and frozen inputs, never invented model statistics.
5. Read the report independently of the AI panel. Refresh re-reads the original request;
   an unknown outcome must not be blindly resubmitted. Candidate acceptance is an evidence
   decision, not execution authorization.

AI configuration has one editing location: **Resources → Model connections**. Configure only a
provider you choose; saving configuration does not authenticate it or make a model request.
Credential storage and cancellation rules are described in [desktop configuration](docs/desktop-macos.md).
No key belongs in an idea, browser storage, screenshot, log or research artifact.

Local simulation uses generated market input and simulated funds. Stopping cuts off new strategy
decisions; it does not imply cancelling every order or closing positions. The separate Mac Paper
operator tooling and drills are retained for existing use: [local Paper](docs/local-paper.md) and
[Mac Paper operations](docs/local-paper-runtime.md). They are not additional QUINE user launch paths.

## Verification and delivery boundary

Shared React components, API contracts and business paths are tested once according to change risk.
The browser is an **internal test carrier**, not a second supported product. Final acceptance uses
the installed desktop for native startup/quit, workspace ownership, window/focus/keyboard/IME and
WebView-specific behavior. A browser pass does not prove these native paths.

The shared React/WebView build, loopback HTTP adapter and `web` dependency group remain in place.
`ea web serve` remains available for internal development, installed integration tests and historical
compatibility; standalone browser/server/mobile products are not supported. Desktop narrow content
areas and text scaling remain supported layout requirements. No IPC migration or duplicate UI is
part of this decision; see [ADR 0050](docs/adr/0050-desktop-only-product-delivery.md).

The historical EA package version remains **0.2.0**; that release is not a public QUINE desktop
release. Nothing here changes package metadata, tags or publication. No real-time, all-market,
robustness, profit, hosted-service or real-money trading claim is made.

[STATUS](docs/STATUS.md) · [Contributing](CONTRIBUTING.md) ·
[Workflow](docs/governance/WORKFLOW.md) · [Desktop release evidence](docs/research-first-release.md)
