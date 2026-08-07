#!/usr/bin/env python3
"""V53.1 pipeline orchestrator.

Per project architecture constraints: uses pathlib.Path for all file pathing,
subprocess.run with sys.executable to trigger local execution of the two
pipeline stages, and a shared JSON file contract (pipeline_status.json) to
exchange status between stages instead of parsing free-form stdout text by
hand. stdout and stderr are captured separately for each stage and written to
per-run log files so failures are diagnosable without re-running anything.

FIX (V53.1, confirmed from production run): the previous version opened the
dashboard URL after a fixed time.sleep(1.5), which raced the local
http.server binding its port on slower machines/disks and produced a 404 in
the browser even though the pipeline itself passed. Replaced with an actual
TCP-connect readiness poll (wait_for_server) up to 10 seconds before opening
the browser, falling back to a direct file:// URI if the server never comes
up in time.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def run_stage(stage_name: str, cmd: list[str], log_dir: Path) -> dict[str, Any]:
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{stage_name}_stdout.log"
    stderr_path = log_dir / f"{stage_name}_stderr.log"
    started_at = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "stage": stage_name, "command": cmd, "started_at_utc": started_at,
        "returncode": None, "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
        "status": "NOT_RUN", "error": None,
    }
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        stdout_path.write_text(proc.stdout or "", encoding="utf-8")
        stderr_path.write_text(proc.stderr or "", encoding="utf-8")
        result["returncode"] = proc.returncode
        result["status"] = "PASS" if proc.returncode == 0 else "FAIL"
        result["stdout_tail"] = (proc.stdout or "")[-4000:]
        result["stderr_tail"] = (proc.stderr or "")[-4000:]
    except subprocess.TimeoutExpired as e:
        stdout_path.write_text(e.stdout or "" if isinstance(e.stdout, str) else "", encoding="utf-8")
        stderr_path.write_text(e.stderr or "" if isinstance(e.stderr, str) else "", encoding="utf-8")
        result["status"] = "FAIL"
        result["error"] = f"TimeoutExpired: stage exceeded {e.timeout}s"
    except FileNotFoundError as e:
        result["status"] = "FAIL"
        result["error"] = f"FileNotFoundError: {e}"
    except Exception as e:
        result["status"] = "FAIL"
        result["error"] = f"{type(e).__name__}: {e}"
    result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def find_latest_run_dir(output_root: Path, requested_date: str) -> Path | None:
    if not output_root.is_dir():
        return None
    candidates = sorted(
        (p for p in output_root.iterdir() if p.is_dir() and p.name.startswith(requested_date)),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return candidates[0] if candidates else None


def start_http_server(run_dir: Path, port: int, log_dir: Path) -> subprocess.Popen | None:
    log_path = log_dir / "http_server_stdout.log"
    try:
        f = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port)],
            cwd=str(run_dir), stdout=f, stderr=subprocess.STDOUT,
        )
        return proc
    except Exception:
        return None


def wait_for_server(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                if sock.connect_ex((host, port)) == 0:
                    return True
            except OSError:
                pass
        time.sleep(0.2)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=str(date.today()))
    ap.add_argument("--output-root", default="mlb_daily_outputs")
    ap.add_argument("--script-dir", default=None, help="Directory containing the pipeline .py files; defaults to this file's directory.")
    ap.add_argument("--open-dashboard", action="store_true")
    ap.add_argument("--http-port", type=int, default=8765)
    args = ap.parse_args()

    script_dir = Path(args.script_dir).resolve() if args.script_dir else Path(__file__).resolve().parent
    output_root = Path(args.output_root)
    logs_dir = Path("pipeline_logs") / f"{args.date}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    main_script = script_dir / "MLB_DAILY_PITCH_ENVIRONMENT_V53.py"
    bundle_script = script_dir / "build_dashboard_bundle_V53.py"

    pipeline_status: dict[str, Any] = {
        "pipeline_version": "1.0.1", "requested_date": args.date,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "stages": [], "overall_status": "RUNNING",
    }

    if not main_script.exists():
        pipeline_status["overall_status"] = "FAIL"
        pipeline_status["error"] = f"Main script not found: {main_script}"
        Path("pipeline_status.json").write_text(json.dumps(pipeline_status, indent=2), encoding="utf-8")
        print(json.dumps(pipeline_status, indent=2))
        return 2

    stage1_cmd = [sys.executable, str(main_script), "--date", args.date, "--output-root", str(output_root)]
    stage1 = run_stage("run_environment_pipeline", stage1_cmd, logs_dir)
    pipeline_status["stages"].append(stage1)

    if stage1["status"] != "PASS":
        pipeline_status["overall_status"] = "FAIL"
        Path("pipeline_status.json").write_text(json.dumps(pipeline_status, indent=2), encoding="utf-8")
        print(json.dumps(pipeline_status, indent=2))
        return 1

    run_dir = find_latest_run_dir(output_root, args.date)
    if run_dir is None:
        pipeline_status["overall_status"] = "FAIL"
        pipeline_status["error"] = f"Stage 1 reported PASS but no run directory found under {output_root} for {args.date}."
        Path("pipeline_status.json").write_text(json.dumps(pipeline_status, indent=2), encoding="utf-8")
        print(json.dumps(pipeline_status, indent=2))
        return 1
    pipeline_status["run_dir"] = str(run_dir.resolve())

    if not bundle_script.exists():
        pipeline_status["overall_status"] = "FAIL"
        pipeline_status["error"] = f"Bundle script not found: {bundle_script}"
        Path("pipeline_status.json").write_text(json.dumps(pipeline_status, indent=2), encoding="utf-8")
        print(json.dumps(pipeline_status, indent=2))
        return 2

    stage2_cmd = [sys.executable, str(bundle_script), str(run_dir)]
    stage2 = run_stage("build_dashboard_bundle", stage2_cmd, logs_dir)
    pipeline_status["stages"].append(stage2)

    if stage2["status"] != "PASS":
        pipeline_status["overall_status"] = "FAIL"
        Path("pipeline_status.json").write_text(json.dumps(pipeline_status, indent=2), encoding="utf-8")
        print(json.dumps(pipeline_status, indent=2))
        return 1

    pipeline_status["overall_status"] = "PASS"
    pipeline_status["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    Path("pipeline_status.json").write_text(json.dumps(pipeline_status, indent=2), encoding="utf-8")
    print(json.dumps(pipeline_status, indent=2))

    if args.open_dashboard:
        dashboard = run_dir / "mlb-pitch-environment-live-dashboard-V53.html"
        if dashboard.exists():
            proc = start_http_server(run_dir, args.http_port, logs_dir)
            if proc is not None and wait_for_server("127.0.0.1", args.http_port, timeout=10.0):
                webbrowser.open(f"http://localhost:{args.http_port}/{dashboard.name}")
            else:
                webbrowser.open(dashboard.resolve().as_uri())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
