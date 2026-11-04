"""Run paired task solvers in fresh, network-isolated processes and score them."""
import argparse
import importlib.metadata
import json
import os
import platform
import re
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from provenance import file_sha256, tree_sha256

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
EVALUATOR = REPO / "evaluation/evaluate.py"
STRATEGIES = ("pandas", "duckdb")
TASK_PATTERN = re.compile(r"task_[1-9][0-9]*\Z")


def children_cpu():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def contained(path, root):
    return path == root or root in path.parents


def verify_manifest(data_root, manifest):
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported manifest version")
    source = manifest.get("source", {})
    if source.get("kind") not in {"official", "synthetic"}:
        raise ValueError("Manifest source.kind must be official or synthetic")
    if source["kind"] == "official":
        if not source.get("dataset_revision") or not re.fullmatch(r"[0-9a-f]{64}", source.get("archive_sha256", "")):
            raise ValueError("Official source requires dataset_revision and published archive_sha256")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Manifest must contain tasks")
    seen = set()
    for entry in tasks:
        task_id = entry.get("task_id", "")
        if not TASK_PATTERN.fullmatch(task_id) or task_id in seen:
            raise ValueError(f"Invalid or duplicate task ID: {task_id!r}")
        seen.add(task_id)
        if not entry.get("case"):
            raise ValueError(f"Missing case implementation for {task_id}")
        if source["kind"] == "official" and entry["case"] == "synthetic_sales":
            raise ValueError("The synthetic example cannot be run as an official task")
        task = data_root / "input" / task_id
        gold = data_root / "output" / task_id / "gold.csv"
        config = data_root / "evaluation" / "configs" / (task_id + ".json")
        for path in (task, gold, config):
            if path.is_symlink() or not contained(path.resolve(), data_root):
                raise ValueError(f"Input path escapes data root or is a symlink: {path}")
        actual = {"input_tree_sha256": tree_sha256(task),
                  "gold_sha256": file_sha256(gold), "config_sha256": file_sha256(config)}
        for key, value in actual.items():
            if entry.get(key) != value:
                raise ValueError(f"Provenance mismatch for {task_id} {key}: got {value}")
    return tasks


def sandbox_command(task, output, case, strategy):
    if sys.platform != "linux" or not shutil.which("bwrap"):
        raise RuntimeError("Linux and bubblewrap are required for isolation")
    prefix, base = Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()
    if prefix == base:
        raise RuntimeError("Use the research virtual environment to run this script")
    # Never mount the repository, data root, home, proxy settings, or gold into
    # the namespace. The sole input mount is this task; the output is temporary.
    command = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
               "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
               "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64"]
    if not contained(base, Path("/usr")):
        command += ["--ro-bind", str(base), str(base)]
    command += ["--ro-bind", str(prefix), "/venv", "--dir", "/experiment",
                "--ro-bind", str(HERE / "worker.py"), "/experiment/worker.py",
                "--ro-bind", str(HERE / "solvers.py"), "/experiment/solvers.py",
                "--ro-bind", str(task), "/input", "--bind", str(output), "/output",
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--chdir", "/output"]
    for key, value in {"HOME": "/tmp", "PATH": "/venv/bin:/usr/bin:/bin",
                       "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                       "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
                       "NUMEXPR_NUM_THREADS": "1"}.items():
        command += ["--setenv", key, value]
    return command + ["/venv/bin/python", "-B", "/experiment/worker.py", "--case", case, "--strategy", strategy]


def evaluate(prediction, data_root, task_id, scratch, timeout):
    staging_start = time.perf_counter()
    prediction_root = scratch / "prediction"
    prediction_dir = prediction_root / task_id
    prediction_dir.mkdir(parents=True)
    shutil.copyfile(prediction, prediction_dir / "prediction.csv")
    config_root = scratch / "config"
    config_root.mkdir()
    shutil.copyfile(data_root / "evaluation/configs" / (task_id + ".json"), config_root / (task_id + ".json"))
    result_path = scratch / "score.json"
    staging_wall = time.perf_counter() - staging_start
    command = [sys.executable, str(EVALUATOR), "--prediction-root", str(prediction_root),
               "--gold-root", str(data_root / "output"), "--config-root", str(config_root), "--output", str(result_path)]
    start_wall, start_cpu = time.perf_counter(), children_cpu()
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "evaluator_timeout", "staging_wall_seconds": staging_wall,
                "wall_seconds": time.perf_counter() - start_wall,
                "cpu_seconds": children_cpu() - start_cpu}
    result = {"staging_wall_seconds": staging_wall, "wall_seconds": time.perf_counter() - start_wall,
              "cpu_seconds": children_cpu() - start_cpu}
    if proc.returncode != 0:
        result.update(status="evaluator_error", error=proc.stderr[-4000:])
        return result
    summary = json.loads(result_path.read_text(encoding="utf-8"))
    if summary["task_count"] != 1 or summary["tasks"][0]["task_id"] != task_id:
        raise RuntimeError("Evaluator scored the wrong task set")
    result.update(status="scored", config_set_sha256=summary["config_set_sha256"], task=summary["tasks"][0])
    return result


def run_one(data_root, entry, strategy, repetition, timeout):
    with tempfile.TemporaryDirectory(prefix="dataspace-research-") as tmp:
        scratch = Path(tmp)
        worker_output = scratch / "worker"
        worker_output.mkdir()
        command = sandbox_command(data_root / "input" / entry["task_id"], worker_output, entry["case"], strategy)
        run = {"task_id": entry["task_id"], "strategy": strategy, "repetition": repetition}
        start_wall, start_cpu = time.perf_counter(), children_cpu()
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            run.update(status="worker_timeout", worker_wall_seconds=time.perf_counter() - start_wall,
                       launcher_cpu_seconds=children_cpu() - start_cpu)
            return run
        run.update(worker_wall_seconds=time.perf_counter() - start_wall,
                   launcher_cpu_seconds=children_cpu() - start_cpu)
        metrics = worker_output / "metrics.json"
        if metrics.exists():
            run["worker"] = json.loads(metrics.read_text(encoding="utf-8"))
        prediction = worker_output / "prediction.csv"
        if proc.returncode or not prediction.is_file() or not metrics.is_file():
            run.update(status="worker_error", error=proc.stderr[-4000:])
            return run
        run["prediction_sha256"] = file_sha256(prediction)
        validation = evaluate(prediction, data_root, entry["task_id"], scratch, timeout)
        run["validation"] = validation
        run["status"] = ("correct" if validation["task"]["passed"] else "incorrect") if validation["status"] == "scored" else validation["status"]
        return run


def summarize(tasks, runs, repetitions):
    table = []
    complete = True
    for entry in tasks:
        row = {"task_id": entry["task_id"], "strategies": {}}
        for strategy in STRATEGIES:
            subset = [r for r in runs if r["task_id"] == entry["task_id"] and r["strategy"] == strategy]
            statuses = [r["status"] for r in subset]
            valid = len(statuses) == repetitions and set(statuses) <= {"correct", "incorrect"} and len(set(statuses)) == 1
            if not valid:
                complete = False
            row["strategies"][strategy] = {
                "outcome": statuses[0] if valid else "unknown_or_inconsistent",
                "trial_statuses": statuses,
                "median_execution_wall_seconds": statistics.median(r["worker"]["execution_wall_seconds"] for r in subset)
                if len(subset) == repetitions and all("worker" in r and "execution_wall_seconds" in r["worker"] for r in subset) else None,
            }
        table.append(row)
    result = {"tasks": table, "complete_and_consistent": complete}
    if complete:
        fixed = {strategy: sum(r["strategies"][strategy]["outcome"] == "correct" for r in table) for strategy in STRATEGIES}
        oracle = sum(any(r["strategies"][s]["outcome"] == "correct" for s in STRATEGIES) for r in table)
        coverage = {name: 0 for name in ("both", "pandas_only", "duckdb_only", "neither")}
        for row in table:
            p = row["strategies"]["pandas"]["outcome"] == "correct"
            d = row["strategies"]["duckdb"]["outcome"] == "correct"
            coverage["both" if p and d else "pandas_only" if p else "duckdb_only" if d else "neither"] += 1
        result.update(fixed_correct_counts=fixed, oracle_correct_count=oracle,
                      coverage_counts=coverage,
                      best_fixed_correct_count=max(fixed.values()),
                      oracle_headroom_tasks=oracle - max(fixed.values()),
                      oracle_headroom_fraction=(oracle - max(fixed.values())) / len(table))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args()
    if args.repetitions < 1 or args.timeout_seconds <= 0:
        parser.error("repetitions and timeout must be positive")
    data_root = args.data_root.resolve(strict=True)
    # /usr and the Python installation are visible to the isolated process.
    if any(contained(data_root, p) for p in (Path("/usr"), Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve())):
        parser.error("data root cannot be inside a directory mounted for the worker runtime")
    output = args.output.resolve()
    if contained(output, data_root):
        parser.error("output must be outside the data root")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    start = time.perf_counter()
    tasks = verify_manifest(data_root, manifest)
    hashing_seconds = time.perf_counter() - start
    runs = []
    for entry in tasks:
        for rep in range(args.repetitions):
            order = STRATEGIES if rep % 2 == 0 else STRATEGIES[::-1]
            for strategy in order:
                trial = run_one(data_root, entry, strategy, rep + 1, args.timeout_seconds)
                runs.append(trial)
                print(f"{entry['task_id']} repetition={rep + 1} {strategy}: {trial['status']}", flush=True)
    # Verify the exact data, gold and config bytes again after the experiment.
    verify_manifest(data_root, manifest)
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    result = {
        "schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": manifest["source"], "manifest_sha256": file_sha256(args.manifest), "task_provenance": tasks,
        "evaluator_sha256": file_sha256(EVALUATOR), "repository_head": commit,
        "implementation_sha256": {p: file_sha256(HERE / p) for p in ("run.py", "worker.py", "solvers.py", "provenance.py")},
        "environment": {"python": sys.version, "platform": platform.platform(),
                        "pandas": importlib.metadata.version("pandas"), "duckdb": importlib.metadata.version("duckdb"),
                        "numpy": importlib.metadata.version("numpy"), "logical_cpus": os.cpu_count(),
                        "threads_per_strategy": 1, "isolation": "bubblewrap: read-only task, network namespace, separate process"},
        "protocol": {"repetitions": args.repetitions, "strategy_order": "alternating each repetition",
                     "timeout_seconds": args.timeout_seconds, "pre_run_provenance_hash_wall_seconds": hashing_seconds},
        "runs": runs, "summary": summarize(tasks, runs, args.repetitions),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Results written to {output}")
    return 0 if result["summary"]["complete_and_consistent"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
