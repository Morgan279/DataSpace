"""Index or selectively extract the official release ZIP using HTTP byte ranges.

Partial extraction checks ZIP member CRCs and records SHA-256 for each extracted
file. It cannot verify the publisher's full-archive SHA-256 without downloading
the entire ZIP. Use only immutable revision URLs for the official dataset.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


class RangeUnavailable(RuntimeError):
    pass


class HTTPRangeReader(io.RawIOBase):
    def __init__(self, url, timeout=30):
        super().__init__()
        self.url, self.timeout, self.position = url, timeout, 0
        self.size, self.etag, self.transferred_bytes, self.requests = None, None, 0, 0
        self._range(0, 0)

    def _range(self, start, end):
        headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity",
                   "User-Agent": "dataspace-research-range-reader/1"}
        if self.etag and not self.etag.startswith("W/"):
            headers["If-Match"] = self.etag
        request = urllib.request.Request(self.url, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise RangeUnavailable(f"HTTP error {exc.code} while requesting an archive range") from exc
        except urllib.error.URLError as exc:
            raise RangeUnavailable(f"Archive range request failed: {exc.reason}") from exc
        with response:
            content_range = response.headers.get("Content-Range", "")
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
            if response.status != 206 or not match:
                raise RangeUnavailable("Server did not return a valid HTTP 206 byte range")
            a, b, total = map(int, match.groups())
            if (a, b) != (start, end) or total <= end or (self.size is not None and total != self.size):
                raise RangeUnavailable("Archive range or total size changed unexpectedly")
            etag = response.headers.get("ETag")
            if self.etag and etag and self.etag != etag:
                raise RangeUnavailable("Archive ETag changed during transfer")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise RangeUnavailable("Server unexpectedly compressed an archive range")
            data = response.read(end - start + 2)
            if len(data) != end - start + 1:
                raise RangeUnavailable("Archive range has the wrong length")
            self.size, self.etag = total, self.etag or etag
            self.requests += 1
            self.transferred_bytes += len(data)
            return data

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        if whence not in (io.SEEK_SET, io.SEEK_CUR, io.SEEK_END):
            raise ValueError("Invalid seek origin")
        target = offset if whence == io.SEEK_SET else self.position + offset if whence == io.SEEK_CUR else self.size + offset
        if target < 0:
            raise ValueError("Negative seek position")
        self.position = target
        return target

    def read(self, size=-1):
        if self.position >= self.size or size == 0:
            return b""
        end = self.size - 1 if size is None or size < 0 else min(self.size - 1, self.position + size - 1)
        data = self._range(self.position, end)
        self.position += len(data)
        return data


def task_files(zf):
    """Find tasks that have input, one official gold, and one frozen config."""
    found = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        patterns = (
            (r"(.*?)input/(task_[1-9][0-9]*)/(.+)", "input"),
            (r"(.*?)output/(task_[1-9][0-9]*)/(gold\.csv)", "gold"),
            (r"(.*?)evaluation/configs/(task_[1-9][0-9]*)(\.json)", "config"),
        )
        for pattern, kind in patterns:
            match = re.fullmatch(pattern, name)
            if match:
                prefix, task_id, suffix = match.groups()
                record = found.setdefault((prefix, task_id), {"input": [], "gold": [], "config": []})
                record[kind].append((suffix, info))
                break
    selected = {}
    for (prefix, task_id), record in found.items():
        suffixes = {x[0] for x in record["input"]}
        if ("task.json" not in suffixes or not any(x.startswith("context/") for x in suffixes)
                or len(record["gold"]) != 1 or len(record["config"]) != 1):
            continue
        if task_id in selected:
            raise ValueError(f"Ambiguous task root in archive for {task_id}")
        selected[task_id] = (prefix, record)
    return selected


def catalog(zf):
    found = task_files(zf)
    result = []
    structured = {".csv", ".json", ".jsonl", ".sqlite", ".sqlite3", ".db", ".parquet"}
    for task_id in sorted(found, key=lambda s: int(s.split("_")[1])):
        _, record = found[task_id]
        context = [info for suffix, info in record["input"] if suffix.startswith("context/")]
        suffix_counts = {}
        for info in context:
            suffix = PurePosixPath(info.filename).suffix.lower() or "[no extension]"
            suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1
        all_files = [info for entries in record.values() for _, info in entries]
        result.append({"task_id": task_id, "context_extensions": suffix_counts,
                       "context_file_count": len(context),
                       "structured_only_by_extension": bool(context) and set(suffix_counts) <= structured,
                       "selected_compressed_bytes": sum(x.compress_size for x in all_files),
                       "selected_uncompressed_bytes": sum(x.file_size for x in all_files)})
    return result


def preview_tasks(zf, task_ids, max_json_bytes=1024 * 1024):
    """Read task descriptions only, without fetching inputs, gold or configs."""
    found = task_files(zf)
    previews = []
    for task_id in task_ids:
        if task_id not in found:
            raise ValueError(f"Task has no complete public reference: {task_id}")
        matches = [info for suffix, info in found[task_id][1]["input"] if suffix == "task.json"]
        if len(matches) != 1 or matches[0].file_size > max_json_bytes:
            raise ValueError(f"Missing, duplicate or oversized task.json: {task_id}")
        data = zf.read(matches[0])  # Reading the complete member also verifies its CRC.
        previews.append({"task_id": task_id, "task_json_sha256": hashlib.sha256(data).hexdigest(),
                         "task": json.loads(data.decode("utf-8-sig"))})
    return previews


def extract_selected(zf, task_ids, dest, max_bytes):
    found = task_files(zf)
    if not task_ids or len(task_ids) != len(set(task_ids)):
        raise ValueError("Select one or more distinct task IDs")
    missing = set(task_ids) - found.keys()
    if missing:
        raise ValueError(f"Tasks without complete public references: {sorted(missing)}")
    dest = dest.resolve()
    if dest.exists():
        raise ValueError("Extraction destination already exists")
    repo = Path(__file__).resolve().parent.parent
    if repo == dest or repo in dest.parents:
        raise ValueError("Extract datasets outside the repository")
    entries = []
    names = set()
    for task_id in task_ids:
        prefix, record = found[task_id]
        for group in record.values():
            for _, info in group:
                relative = PurePosixPath(info.filename[len(prefix):])
                mode = info.external_attr >> 16
                if relative.is_absolute() or ".." in relative.parts or stat.S_ISLNK(mode) or info.flag_bits & 1:
                    raise ValueError(f"Unsupported or unsafe ZIP member: {relative}")
                if str(relative) in names:
                    raise ValueError(f"Duplicate ZIP member: {relative}")
                names.add(str(relative))
                entries.append((relative, info))
    total = sum(info.file_size for _, info in entries)
    if total > max_bytes:
        raise ValueError(f"Selection is {total} uncompressed bytes; limit is {max_bytes}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    hashes = []
    with tempfile.TemporaryDirectory(prefix=".dataspace-extract-", dir=dest.parent) as temp:
        stage = Path(temp) / "data"
        stage.mkdir()
        for relative, info in entries:
            target = stage.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            # zipfile verifies each member's CRC-32 when fully read.
            with zf.open(info) as source, target.open("xb") as sink:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
                    sink.write(block)
            hashes.append({"path": str(relative), "bytes": info.file_size, "sha256": digest.hexdigest(),
                           "zip_crc32": f"{info.CRC:08x}"})
        os.rename(stage, dest)
    return hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Official HTTPS resolve URL pinned to a dataset commit")
    parser.add_argument("--revision", required=True, help="Immutable Hugging Face dataset commit SHA")
    parser.add_argument("--output", type=Path, required=True, help="Write index or provenance JSON here")
    parser.add_argument("--extract", type=Path, help="New dataset directory outside this checkout")
    parser.add_argument("--tasks", nargs="*", default=[])
    parser.add_argument("--preview-tasks", nargs="*", default=[], help="Read selected task.json descriptions without gold")
    parser.add_argument("--archive-sha256", help="Published ZIP SHA-256, required for extraction")
    parser.add_argument("--max-extract-bytes", type=int, default=1024**3)
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.url)
    expected = f"/datasets/HKUSTDial/DataSpace/resolve/{args.revision}/release/DataSpace-Benchmark.zip"
    if parsed.scheme != "https" or parsed.hostname != "huggingface.co" or parsed.path != expected or not re.fullmatch(r"[0-9a-f]{40,64}", args.revision):
        parser.error("Use the official Hugging Face release URL with an immutable commit SHA")
    if args.extract and not re.fullmatch(r"[0-9a-f]{64}", args.archive_sha256 or ""):
        parser.error("Extraction requires the published archive SHA-256")
    if bool(args.extract) != bool(args.tasks) or args.max_extract_bytes <= 0:
        parser.error("Supply --extract and --tasks together; size limit must be positive")
    with HTTPRangeReader(args.url) as reader, zipfile.ZipFile(reader) as zf:
        result = {"source": {"kind": "official", "dataset": "HKUSTDial/DataSpace", "dataset_revision": args.revision,
                             "archive_sha256": args.archive_sha256, "archive_sha256_verified": False,
                             "zip_size_bytes": reader.size, "etag": reader.etag},
                  "public_reference_candidates": catalog(zf)}
        if args.preview_tasks:
            result["task_previews"] = preview_tasks(zf, args.preview_tasks)
        if args.extract:
            result["extracted_files"] = extract_selected(zf, args.tasks, args.extract, args.max_extract_bytes)
            result["selected_tasks"] = args.tasks
        result["transfer"] = {"http_requests": reader.requests, "archive_bytes_received": reader.transferred_bytes}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}; received {result['transfer']['archive_bytes_received']} archive bytes")


if __name__ == "__main__":
    try:
        main()
    except (RangeUnavailable, ValueError, OSError, zipfile.BadZipFile) as exc:
        print(f"Archive access failed: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
