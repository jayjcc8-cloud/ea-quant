# ADR 0043: Evidence-bound Research Candidate Runtime V1

Date: 2026-09-20

## Status

Accepted for implementation — Issue #208, under the Product Owner's full staged delivery request.

## Decision

Implement the identity, evidence, atomic persistence and three-state research decision contract
proposed in ADR 0039. This decision supersedes its implementation deferral; Accepted historical
ADRs remain unchanged. EVALUATED means the explicitly selected evidence set is complete, not a
statistical claim. ACCEPTED is a human research decision and grants no execution permission.

Eligibility includes delivered Web job V3/V4/V5 with matching nominal Report V1/V2/V3 and Path
V1/V2/V3, respectively. Both endpoints must use the same strategy implementation, normalized
parameters, EA code and distribution. Use exactly one successful source and its explicitly
selected existing chronological Holdout. Legacy jobs lacking snapshots or paths are ineligible.

Fresh persisted job, snapshot, report, path, frozen package and relationship evidence must agree
at creation, detail read and terminal decision. Read existing captured evidence without executing
strategy code or reopening the original CSV. Missing or changed evidence makes a Candidate
unavailable and cannot be accepted. A corrupt candidate cannot prevent unrelated history reads.

Persist one canonical record at workspace/candidates/UUID.json under the existing service lock.
Keep the UUID distinct from the domain-separated immutable evidence fingerprint. State and its
reason are published atomically in one file. Exact terminal retries preserve the original time;
conflicting updates reject. Do not store an editable second copy of reports, data or parameters.

The Web surface provides explicit Holdout selection from Run Detail, Candidate history/detail,
and a reason field for ACCEPTED or REJECTED. No automatic ranking, thresholds, paper/live
promotion, execution authorization, new database, remote service or recovery frontier is added.

## Validation

Issue #208 defines identity, cross-evidence mismatch, strict decoding, legal/retried decisions,
atomic failure/restart and installed-wheel Chromium acceptance. One primary T1 review and normal
CI apply. Runtime completion is determined by merged implementation and verification evidence.
