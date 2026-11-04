# DataSpace execution-strategy feasibility study

The current result is **blocked on access to the public-reference dataset**. No
official task has been selected or scored. See [REPORT.md](REPORT.md). The only
implemented pair of solvers and the checked-in result use a generated synthetic
CSV; they test the measurement and evaluator plumbing, not the research
hypothesis. No model API or model baseline is involved.

## Environment and checks

From the repository root on Linux, with Python 3.12 and Bubblewrap available:

```sh
python3 -m venv research/.venv
research/.venv/bin/pip install -r research/requirements.txt
research/.venv/bin/pip check
python3 -m unittest discover -s evaluation/tests -v
research/.venv/bin/python -m unittest discover -s research/tests -v
```

The runner requires unprivileged user and network namespaces. In this cloud
environment, Bubblewrap needs to run outside the command sandbox. Use the
environment's supported escalation route; do not remove isolation as a
workaround. No API credentials are required. To reproduce the small synthetic
check, choose a new path that does not already exist:

```sh
research/.venv/bin/python research/make_demo.py --output /tmp/dataspace-example
research/.venv/bin/python research/run.py \
  --data-root /tmp/dataspace-example \
  --manifest /tmp/dataspace-example/manifest.json \
  --output /tmp/dataspace-synthetic-results.json --repetitions 3
```

The generated `task_1` is synthetic and is explicitly labeled as such in both
manifest and result. It is unrelated to official DataSpace `task_1`. The checked
in [synthetic.json](results/synthetic.json) records one run of these commands.

## Accessing official data after network access is configured

The upstream README identifies the release as a ZIP at Hugging Face and gives
a separate SHA-256 file. Its documented 15 GB is the size of task context,
not a verified ZIP download size. In this environment access to
`huggingface.co` currently returns proxy HTTP 403. The environment draft has a
request to allow that domain; review and save it in environment settings. A
redirect may reveal that additional official storage hosts are also necessary.

Once accessible, resolve the dataset's immutable revision and retrieve the
published checksum without downloading the ZIP:

```sh
revision=$(curl -fsSL https://huggingface.co/api/datasets/HKUSTDial/DataSpace | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')
release_url="https://huggingface.co/datasets/HKUSTDial/DataSpace/resolve/$revision/release"
curl -fsSL "$release_url/DataSpace-Benchmark.zip.sha256" -o /tmp/dataspace-archive.sha256
archive_sha256=$(awk 'NR == 1 {print $1}' /tmp/dataspace-archive.sha256)
research/.venv/bin/python research/archive.py \
  --url "$release_url/DataSpace-Benchmark.zip" --revision "$revision" \
  --archive-sha256 "$archive_sha256" --output /tmp/dataspace-index.json
```

The index lists only ZIP members that have an input, gold CSV, and frozen
config. It counts file types and compressed/uncompressed bytes. The
`structured_only_by_extension` flag is a screening heuristic, not a semantic
judgment: inspect questions and workspaces before selecting tasks. To examine
questions without fetching gold, rerun the command with, for example,
`--preview-tasks task_1 task_2`; use task IDs from the actual index, not this
illustration. The output contains task descriptions and should remain outside
the repository.

After selecting 10–20 suitable tasks, extract exactly those tasks, their gold
files, and frozen configs to a **new directory outside this checkout**:

```sh
research/.venv/bin/python research/archive.py \
  --url "$release_url/DataSpace-Benchmark.zip" --revision "$revision" \
  --archive-sha256 "$archive_sha256" --output /tmp/dataspace-extraction.json \
  --extract /tmp/dataspace-selected --tasks task_1 task_2
```

Replace those illustrative IDs with the actual selection. The default limit is
1 GiB uncompressed; `--max-extract-bytes` changes it deliberately. Extraction
uses a staging directory, rejects unsafe paths, and checks each member's ZIP
CRC while recording a SHA-256 for each extracted file. The range reader has
been tested with a local mocked HTTP server, **not with Hugging Face**. If the
actual server ignores ranges, it fails rather than silently downloading the
archive; use the upstream full-download procedure instead. Even when ranges
work, verifying the publisher's **full archive SHA-256** requires downloading
the full archive. Partial extraction records the published hash as unverified;
HTTPS, the pinned revision, ZIP CRCs, and per-file hashes do not constitute a
full-archive checksum check.

## Completing the comparison

Freeze the cohort and selection criteria before any scoring, with diversity in
formats, operations, and data sizes; keep failed and unsupported cases. Implement
one Pandas and one DuckDB solution per selected task in `solvers.py`, using the
question and input only. The two implementations should have the same answer
contract and must not read gold. Do not tune on held-out references: separate
development tasks and freeze test solutions and any router before test scoring.
After implementing a pair,
bind its name to the extracted task and its hashes. For example:

```sh
research/.venv/bin/python research/make_manifest.py \
  --data-root /tmp/dataspace-selected --extraction-provenance /tmp/dataspace-extraction.json \
  --case task_1=task_1 --case task_2=task_2 --output /tmp/dataspace-manifest.json
research/.venv/bin/python research/run.py \
  --data-root /tmp/dataspace-selected --manifest /tmp/dataspace-manifest.json \
  --output /tmp/dataspace-results.json --repetitions 3
```

The example IDs and case names are placeholders, not implemented official
solutions. Add a `--case` for each selected task. The manifest tool checks the
exact extracted file set and member hashes. The runner verifies hashes for the
whole task input tree, gold, and config both before and after the run. A worker
sees only its read-only task input, read-only runtime and solver code, and a
writable temporary output; Bubblewrap clears the environment and unshares the
network. The host, not the worker, runs the unchanged official evaluator using
the original gold and a copy of the one selected config. Data and predictions
are not copied into this repository by these commands.

Each task gets both strategies in fresh processes, with alternating order over
repetitions and one thread per strategy. JSON results retain trial status,
prediction hash, evaluator result, solver setup and execution wall/CPU time,
whole-worker wall time, whole-worker CPU time and peak RSS, evaluator wall/CPU
time, staging time, source and code hashes, and environment versions. Peak RSS
is the Linux process high-water mark and includes the interpreter and imports;
it is not incremental memory. `launcher_cpu_seconds` accounts for the
Bubblewrap launcher, not the worker; use `worker.process_cpu_seconds` for the
worker. Validation time covers evaluator startup and scoring, with staging
separately measured. Dependency installation time is not recorded. Filesystem
caches are not flushed, and results are sequential, not randomized experiments.

The summary counts tasks solved by both, either only, or neither; the empirical
oracle is the union of tasks solved by at least one strategy. Its headroom is
the oracle count minus the better fixed strategy count, divided by the number
of selected tasks. The oracle chooses after seeing outcomes and is an upper
bound, not a learned selection policy. Infrastructure errors or inconsistent
repetitions are reported as unknown and suppress the aggregate oracle estimate.
