# ADR 0037: Registered Local OHLCV Research Input V1

Date: 2026-09-08

## Status

Accepted — Issue #187

## Decision

An optional explicit absolute `--data-root` authorizes a local directory-backed, non-persistent
registry. Only immediate regular `.csv` children with bounded filename identifiers are admitted;
no recursion, symlink traversal, implicit scan, uploads, URLs or network sources. Data, scenario,
workspace, UI and strategy roots remain separate. Absence of the option preserves legacy inputs.
The filesystem is trusted local input; clients submit dataset identifiers, never filesystem paths.
Malformed entries have safe catalog summaries and cannot invalidate unrelated entries.

V1 reuses the strict `ea-phase1-ohlcv-csv-v1` decoder and historical validation. The smallest
full-capture ReplayWindow starts at minimum admitted `available_at` and ends one microsecond after
maximum `available_at`, matching the existing half-open admission contract. Unrepresentable end
boundaries reject. A registered capture must contain exactly one scenario-compatible instrument.
There is no date editor, transformation, automatic split, symbol editor or instrument registry.

Filename `dataset_id` is only a catalog/reference identity. SHA-256 of validated original bytes is
source provenance. The existing canonical data fingerprint, record count and replay window remain
economic identity. Data-only scenario derivation invokes existing complete scenario validation;
all other economic configuration stays unchanged. Paths, filenames and representation-only source
changes do not enter scenario semantic identity. Scenario V1/V2/V3 domains are unchanged; no V4.

Validation captures immutable in-memory dataset evidence. Acceptance verifies the selected raw
source digest again; execution uses the accepted capture and fails closed on detected replacement.
Batch members share one selected dataset identity, with separate existing strategy parameter maps.
No latest-content fallback is permitted when history reuse presents a previous source digest.

New Web input snapshots use `ea.local-web-input.v2` to add dataset ID, raw SHA, canonical data SHA,
record count and derived window beside the derived canonical scenario and its digest. Legacy
snapshots remain readable. External absolute paths are excluded from this semantic snapshot.
Completed history, comparison and Holdout read persisted snapshots and hash-verified formal
reports, including after external deletion or replacement. There is no raw-data-per-job copying
requirement, content-addressed data store, historical migration or new recovery frontier. New
execution and CLI report regeneration/resume may require the original external capture.

For registered-data chronological Holdout, the source completed job supplies the frozen economic
contract, implementation artifact and parameters; a manually selected compatible later capture
replaces only data. Exact frozen local strategy bytes remain authoritative even if the configured
artifact is deleted. Existing strict chronology and report evidence remain mandatory. The claim
is chronological holdout evaluation, without unseen-data, unbiased-validation, generalization or
statistical pass/fail claims.

`ea data inspect PATH` provides canonical JSON for strict full-capture inspection without manual
fingerprint calculation. No additional data commands or data formats are introduced.

## Validation

Targeted identity/provenance, malformed input, path boundary, replacement, legacy scenario and
local strategy tests accompany real checkout-external installed-wheel Chromium single run,
history/reuse, two-member batch, comparison, Holdout, restart and changed-content conflict proof.
One primary T1 review and existing CI apply. No new governance or activation of #125.
