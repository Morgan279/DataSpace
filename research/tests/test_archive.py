import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archive


def archive_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("DataSpace-Benchmark/input/task_1/task.json", "{}")
        zf.writestr("DataSpace-Benchmark/input/task_1/context/a.csv", "x\n1\n")
        zf.writestr("DataSpace-Benchmark/output/task_1/gold.csv", "x\n1\n")
        zf.writestr("DataSpace-Benchmark/evaluation/configs/task_1.json", "{}")
        zf.writestr("DataSpace-Benchmark/input/task_2/task.json", "{}")
        zf.writestr("DataSpace-Benchmark/input/task_2/context/a.csv", "x\n1\n")
        # An unselected file ensures a range-based index can avoid a full ZIP.
        zf.writestr("other/large.bin", b"x" * 1024 * 1024)
    return output.getvalue()


class Response(io.BytesIO):
    def __init__(self, body, status, headers):
        super().__init__(body)
        self.status, self.headers = status, headers


class ArchiveRangeTests(unittest.TestCase):
    def setUp(self):
        self.contents = archive_bytes()
        self.etag = '"stable"'

    def serve_range(self, request, timeout):
        requested = request.get_header("Range")
        start, end = map(int, requested.removeprefix("bytes=").split("-"))
        headers = {"Content-Range": f"bytes {start}-{end}/{len(self.contents)}", "ETag": self.etag}
        return Response(self.contents[start:end + 1], 206, headers)

    def test_range_index_and_selective_extract(self):
        with patch.object(urllib.request, "urlopen", side_effect=self.serve_range):
            with archive.HTTPRangeReader("https://example.invalid/data.zip") as reader:
                with zipfile.ZipFile(reader) as zf:
                    candidates = archive.catalog(zf)
                    self.assertEqual([c["task_id"] for c in candidates], ["task_1"])
                    self.assertTrue(candidates[0]["structured_only_by_extension"])
                    self.assertLess(reader.transferred_bytes, len(self.contents))
                    preview = archive.preview_tasks(zf, ["task_1"])
                    self.assertEqual(preview[0]["task"], {})
                    self.assertEqual(len(preview[0]["task_json_sha256"]), 64)
                    with tempfile.TemporaryDirectory() as temp:
                        dest = Path(temp) / "release"
                        with self.assertRaisesRegex(ValueError, "limit"):
                            archive.extract_selected(zf, ["task_1"], dest, 1)
                        files = archive.extract_selected(zf, ["task_1"], dest, 10000)
                        self.assertEqual(len(files), 4)
                        self.assertEqual((dest / "input/task_1/context/a.csv").read_text(), "x\n1\n")
                        self.assertEqual(json.loads((dest / "evaluation/configs/task_1.json").read_text()), {})
                        with self.assertRaisesRegex(ValueError, "already exists"):
                            archive.extract_selected(zf, ["task_1"], dest, 10000)

    def test_server_ignoring_ranges_is_rejected(self):
        with patch.object(urllib.request, "urlopen", return_value=Response(b"x", 200, {})):
            with self.assertRaises(archive.RangeUnavailable):
                archive.HTTPRangeReader("https://example.invalid/data.zip")

    def test_corrupt_member_fails_crc_and_does_not_publish_partial_data(self):
        damaged = bytearray(self.contents)
        position = damaged.index(b"x\n1\n")
        damaged[position] ^= 1
        self.contents = bytes(damaged)
        with patch.object(urllib.request, "urlopen", side_effect=self.serve_range):
            with archive.HTTPRangeReader("https://example.invalid/data.zip") as reader:
                with zipfile.ZipFile(reader) as zf:
                    with tempfile.TemporaryDirectory() as temp:
                        dest = Path(temp) / "release"
                        with self.assertRaises(zipfile.BadZipFile):
                            archive.extract_selected(zf, ["task_1"], dest, 10000)
                        self.assertFalse(dest.exists())

    def test_path_traversal_member_is_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("input/task_1/task.json", "{}")
            zf.writestr("input/task_1/context/../../escape", "bad")
            zf.writestr("output/task_1/gold.csv", "x\n")
            zf.writestr("evaluation/configs/task_1.json", "{}")
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    archive.extract_selected(zf, ["task_1"], Path(temp) / "out", 10000)

    def test_changed_etag_is_rejected(self):
        with patch.object(urllib.request, "urlopen", side_effect=self.serve_range):
            reader = archive.HTTPRangeReader("https://example.invalid/data.zip")
            self.etag = '"changed"'
            with self.assertRaisesRegex(archive.RangeUnavailable, "ETag changed"):
                reader.read(1)
            reader.close()

    def test_official_manifest_checks_extraction_hashes_and_file_set(self):
        with patch.object(urllib.request, "urlopen", side_effect=self.serve_range):
            with archive.HTTPRangeReader("https://example.invalid/data.zip") as reader:
                with zipfile.ZipFile(reader) as zf:
                    with tempfile.TemporaryDirectory() as temp:
                        directory = Path(temp)
                        root = directory / "dataset"
                        files = archive.extract_selected(zf, ["task_1"], root, 10000)
                        provenance = directory / "extraction.json"
                        provenance.write_text(json.dumps({"source": {"kind": "official", "dataset_revision": "a" * 40,
                                                                      "archive_sha256": "b" * 64,
                                                                      "archive_sha256_verified": False},
                                                          "selected_tasks": ["task_1"], "extracted_files": files}))
                        manifest = directory / "manifest.json"
                        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "make_manifest.py"),
                                   "--data-root", str(root), "--extraction-provenance", str(provenance),
                                   "--case", "task_1=example", "--output", str(manifest)]
                        completed = subprocess.run(command, capture_output=True, text=True)
                        self.assertEqual(completed.returncode, 0, completed.stderr)
                        self.assertEqual(json.loads(manifest.read_text())["tasks"][0]["task_id"], "task_1")
                        (root / "input/task_1/context/extra.csv").write_text("x\n")
                        completed = subprocess.run(command, capture_output=True, text=True)
                        self.assertNotEqual(completed.returncode, 0)
                        self.assertIn("file set changed", completed.stderr)


if __name__ == "__main__":
    unittest.main()
