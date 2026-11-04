"""One desktop-owned child. Parent EOF only requests existing cooperative stop."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from threading import Event, Thread

from ea.core import RunId
from ea.product.paper_run import run_local_paper
from ea.web.paper_desktop import DesktopPaper
from ea.web.quine import QuineBridge


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--quine-socket", type=Path)
    parser.add_argument("--quine-workspace", type=Path)
    args = parser.parse_args()
    quine = (
        None
        if args.quine_socket is None
        else QuineBridge(args.quine_socket, workspace=args.quine_workspace)
    )
    manager = DesktopPaper(args.root, args.workspace, quine=quine)
    record = manager._record(args.request_id)
    request = record["request"]
    stopping = Event()

    def parent_pipe() -> None:
        # Avoid holding a buffered stdin lock when a duration stop exits before EOF.
        while os.read(sys.stdin.fileno(), 4096):
            pass
        stopping.set()

    Thread(target=parent_pipe, daemon=True).start()
    # A stop during startup is durable even if this process has not read the pipe yet.
    if record.get("stop_requested"):
        stopping.set()
    result = run_local_paper(
        args.workspace,
        request["candidate_id"],
        manager.requests / f"{args.request_id}.scenario",
        manager.runs,
        run_id=RunId(args.request_id),
        prices=tuple(request["prices"]),
        interval=request["interval_seconds"],
        duration_seconds=request.get("duration_seconds"),
        evidence_reader=manager.reader(),
        parent_stop=stopping.is_set,
        expected_fingerprint=request["fingerprint"],
    )
    print(json.dumps({"state": result["state"], "run_id": args.request_id}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
