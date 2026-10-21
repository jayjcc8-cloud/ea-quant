"""Read-only macOS distribution preflight; no signing, upload or release approval.

Checks one bundle against explicitly supplied source, version and team. Passing
these checks does not replace research acceptance or installation on a clean Mac.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any


def run(argv: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return False, type(error).__name__
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def check(app: Path, source: str, version: str, team: str | None) -> dict[str, Any]:
    app = app.resolve()
    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, passed: bool, detail: str) -> None:
        checks[name] = {"passed": passed, "detail": detail}

    try:
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        build = json.loads((app / "Contents/Resources/runtime/build-info.json").read_text())
        record(
            "source",
            build.get("source_revision") == source and build.get("source_dirty") is False,
            "requires exact source revision and source_dirty=false",
        )
        record(
            "version",
            info.get("CFBundleShortVersionString") == version
            and build.get("desktop_version") == version,
            "bundle and runtime versions must both match",
        )
        record(
            "bundle",
            info.get("CFBundleIdentifier") == "org.quine.research.desktop",
            "QUINE bundle identity",
        )
    except (OSError, ValueError, TypeError, AttributeError) as error:
        record("metadata", False, f"unreadable metadata: {type(error).__name__}")
    verified, detail = run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
    record("signature_integrity", verified, detail)
    displayed, detail = run(["/usr/bin/codesign", "-dv", "--verbose=4", str(app)])
    record(
        "hardened_runtime",
        displayed and bool(re.search(r"flags=0x[0-9a-fA-F]+\([^\n)]*\bruntime\b", detail)),
        "main executable must carry the runtime code-signing flag",
    )
    if team is None or re.fullmatch(r"[A-Z0-9]{10}", team) is None:
        record(
            "developer_id", False, "an explicitly confirmed 10-character Apple team ID is required"
        )
    else:
        # Apple anchor + Developer ID issuer/application certificate OIDs + exact team.
        requirement = (
            "=anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists"
            " and certificate leaf[field.1.2.840.113635.100.6.1.13] exists"
            f' and certificate leaf[subject.OU] = "{team}"'
        )
        trusted, detail = run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", requirement, str(app)]
        )
        record("developer_id", trusted, detail)
    if verified and checks["developer_id"]["passed"]:
        stapled, detail = run(["/usr/bin/xcrun", "stapler", "validate", str(app)])
        record("stapled_notarization", stapled, detail)
    else:
        record(
            "stapled_notarization",
            False,
            "not checked: verified Developer ID identity is required first",
        )
    return {
        "app": str(app),
        "distribution_checks_pass": all(c["passed"] for c in checks.values()),
        "checks": checks,
        "release_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-team-id")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_source_sha):
        parser.error("expected source SHA must be 40 lowercase hexadecimal characters")
    result = check(args.app, args.expected_source_sha, args.expected_version, args.expected_team_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["distribution_checks_pass"] else 1)


if __name__ == "__main__":
    main()
