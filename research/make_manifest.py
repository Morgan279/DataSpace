"""Bind explicitly implemented official tasks to exact extracted data bytes."""
import argparse
import json
import re
from pathlib import Path

from provenance import file_sha256, tree_sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--extraction-provenance", type=Path, required=True)
    parser.add_argument("--case", action="append", required=True, help="task_N=implementation_name; repeat for each task")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.data_root.resolve(strict=True)
    output = args.output.resolve()
    if output == root or root in output.parents:
        parser.error("Manifest must be written outside the data root")
    provenance = json.loads(args.extraction_provenance.read_text(encoding="utf-8"))
    source = provenance.get("source", {})
    if source.get("kind") != "official" or not provenance.get("extracted_files"):
        parser.error("Expected official extraction provenance from archive.py")
    seen = set()
    task_entries = []
    for case in args.case:
        task_id, separator, implementation = case.partition("=")
        if (not separator or not re.fullmatch(r"task_[1-9][0-9]*", task_id) or not implementation
                or task_id in seen or task_id not in provenance.get("selected_tasks", [])):
            parser.error(f"Invalid, duplicate, or unextracted case: {case}")
        seen.add(task_id)
        files = [f for f in provenance["extracted_files"] if f["path"].startswith(f"input/{task_id}/")
                 or f["path"] in {f"output/{task_id}/gold.csv", f"evaluation/configs/{task_id}.json"}]
        if not files:
            parser.error(f"No extraction file hashes for {task_id}")
        expected = {f["path"] for f in files}
        task_dir = root / "input" / task_id
        actual = {p.relative_to(root).as_posix() for p in task_dir.rglob("*") if p.is_file()}
        actual.update({f"output/{task_id}/gold.csv", f"evaluation/configs/{task_id}.json"})
        if expected != actual:
            parser.error(f"Extracted file set changed for {task_id}")
        for entry in files:
            path = (root / entry["path"]).resolve()
            if root not in path.parents or not path.is_file() or file_sha256(path) != entry["sha256"]:
                parser.error(f"Extracted file mismatch: {entry['path']}")
        task_entries.append({"task_id": task_id, "case": implementation,
                             "input_tree_sha256": tree_sha256(root / "input" / task_id),
                             "gold_sha256": file_sha256(root / "output" / task_id / "gold.csv"),
                             "config_sha256": file_sha256(root / "evaluation/configs" / (task_id + ".json"))})
    manifest = {"schema_version": 1, "source": dict(source, extraction_provenance_sha256=file_sha256(args.extraction_provenance)),
                "tasks": task_entries}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote manifest for {len(task_entries)} tasks: {output}")


if __name__ == "__main__":
    main()
