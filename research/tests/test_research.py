import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import run
from provenance import file_sha256


class SummaryTests(unittest.TestCase):
    def test_oracle_and_complementarity_are_paired_by_task(self):
        tasks = [{"task_id": "task_1"}, {"task_id": "task_2"}, {"task_id": "task_3"}]
        outcomes = {"task_1": ("correct", "incorrect"), "task_2": ("incorrect", "correct"),
                    "task_3": ("incorrect", "incorrect")}
        runs = [{"task_id": task_id, "strategy": strategy, "status": status}
                for task_id, pair in outcomes.items() for strategy, status in zip(run.STRATEGIES, pair)]
        result = run.summarize(tasks, runs, 1)
        self.assertEqual(result["fixed_correct_counts"], {"pandas": 1, "duckdb": 1})
        self.assertEqual(result["oracle_correct_count"], 2)
        self.assertEqual(result["oracle_headroom_tasks"], 1)
        self.assertAlmostEqual(result["oracle_headroom_fraction"], 1 / 3)
        self.assertEqual(result["coverage_counts"], {"both": 0, "pandas_only": 1, "duckdb_only": 1, "neither": 1})

    def test_infrastructure_error_or_inconsistent_repeats_do_not_become_failures(self):
        tasks = [{"task_id": "task_1"}]
        runs = [{"task_id": "task_1", "strategy": "pandas", "status": "correct"},
                {"task_id": "task_1", "strategy": "duckdb", "status": "worker_error"}]
        result = run.summarize(tasks, runs, 1)
        self.assertFalse(result["complete_and_consistent"])
        self.assertNotIn("oracle_correct_count", result)
        runs[1]["status"] = "correct"
        runs.extend([dict(runs[0], status="incorrect"), dict(runs[1])])
        self.assertFalse(run.summarize(tasks, runs, 2)["complete_and_consistent"])


class RunnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "fixture"
        subprocess.run([sys.executable, str(HERE / "make_demo.py"), "--output", str(self.root)],
                       check=True, capture_output=True, text=True)
        self.manifest = json.loads((self.root / "manifest.json").read_text())

    def test_manifest_rejects_changed_inputs_and_gold(self):
        run.verify_manifest(self.root, self.manifest)
        sales = self.root / "input/task_1/context/sales.csv"
        original = sales.read_bytes()
        sales.write_bytes(original + b"North,food,10\n")
        with self.assertRaisesRegex(ValueError, "input_tree_sha256"):
            run.verify_manifest(self.root, self.manifest)
        sales.write_bytes(original)
        (self.root / "output/task_1/gold.csv").write_text("wrong\n")
        with self.assertRaisesRegex(ValueError, "gold_sha256"):
            run.verify_manifest(self.root, self.manifest)

    def test_sandbox_hides_gold_and_credentials_and_makes_input_read_only(self):
        output = Path(self.temporary.name) / "worker"
        output.mkdir()
        cmd = run.sandbox_command(self.root / "input/task_1", output, "synthetic_sales", "pandas")
        cmd = cmd[:cmd.index("/venv/bin/python")] + ["/venv/bin/python", "-c", "\n".join([
            "import os, pathlib",
            "assert pathlib.Path('/input/context/sales.csv').exists()",
            f"assert not pathlib.Path({str(self.root / 'output/task_1/gold.csv')!r}).exists()",
            "assert 'DATASPACE_TEST_SECRET' not in os.environ",
            "assert 'eth0:' not in pathlib.Path('/proc/net/dev').read_text()",
            "try:",
            " pathlib.Path('/input/forbidden').write_text('x')",
            " raise AssertionError('input was writable')",
            "except OSError:",
            " pass",
        ])]
        test_env = dict(os.environ, DATASPACE_TEST_SECRET="test-only-sentinel")
        completed = subprocess.run(cmd, capture_output=True, text=True, env=test_env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse((self.root / "input/task_1/forbidden").exists())

    def test_two_strategies_are_scored_by_official_evaluator(self):
        result_path = Path(self.temporary.name) / "results.json"
        completed = subprocess.run([sys.executable, str(HERE / "run.py"), "--data-root", str(self.root),
                                    "--manifest", str(self.root / "manifest.json"), "--output", str(result_path),
                                    "--repetitions", "1"], capture_output=True, text=True, timeout=45)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(result_path.read_text())
        self.assertEqual(result["source"]["kind"], "synthetic")
        self.assertEqual([r["status"] for r in result["runs"]], ["correct", "correct"])
        self.assertEqual(result["summary"]["coverage_counts"]["both"], 1)
        for trial in result["runs"]:
            self.assertGreater(trial["worker"]["process_cpu_seconds"], 0)
            self.assertGreater(trial["worker"]["peak_rss_kib"], 0)
            self.assertGreater(trial["validation"]["wall_seconds"], 0)
            self.assertEqual(trial["validation"]["task"]["task_id"], "task_1")

    def test_wrong_predictions_are_reported_as_incorrect(self):
        gold = self.root / "output/task_1/gold.csv"
        gold.write_text("region,amount\nNorth,999\nSouth,7\n")
        self.manifest["tasks"][0]["gold_sha256"] = file_sha256(gold)
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(self.manifest))
        result_path = Path(self.temporary.name) / "incorrect.json"
        completed = subprocess.run([sys.executable, str(HERE / "run.py"), "--data-root", str(self.root),
                                    "--manifest", str(manifest_path), "--output", str(result_path),
                                    "--repetitions", "1"], capture_output=True, text=True, timeout=45)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(result_path.read_text())
        self.assertEqual([r["status"] for r in result["runs"]], ["incorrect", "incorrect"])
        self.assertEqual(result["summary"]["coverage_counts"]["neither"], 1)


if __name__ == "__main__":
    unittest.main()
