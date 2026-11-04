"""Runs inside an isolated bubblewrap namespace with only one task mounted."""
import argparse
import json
import resource
import time
import traceback
from pathlib import Path

from solvers import setup


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--strategy", choices=("pandas", "duckdb"), required=True)
    args = parser.parse_args()
    result = {"status": "worker_error"}
    try:
        start_wall, start_cpu = time.perf_counter(), time.process_time()
        solve = setup(args.case, args.strategy)
        setup_wall = time.perf_counter() - start_wall
        setup_cpu = time.process_time() - start_cpu
        start_wall, start_cpu = time.perf_counter(), time.process_time()
        solve(Path("/input"), Path("/output/prediction.csv"))
        result.update(
            status="ok",
            setup_wall_seconds=setup_wall,
            setup_cpu_seconds=setup_cpu,
            execution_wall_seconds=time.perf_counter() - start_wall,
            execution_cpu_seconds=time.process_time() - start_cpu,
        )
    except Exception:
        result["error"] = traceback.format_exc(limit=8)
    # Linux reports KiB. This is peak RSS for the full worker process, including
    # Python startup and imports; it is not a delta from an empty interpreter.
    result["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result["process_cpu_seconds"] = time.process_time()
    Path("/output/metrics.json").write_text(json.dumps(result), encoding="utf-8")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
