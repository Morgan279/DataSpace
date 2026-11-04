"""Content identifiers shared by the manifest generator and runner."""
import hashlib
from pathlib import Path


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(root):
    """Hash sorted relative paths, file sizes, and content; reject symlinks."""
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"Not a regular task directory: {root}")
    digest = hashlib.sha256(b"dataspace-task-tree-v1\0")
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError(f"Symlinks are not allowed in task inputs: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"Unsupported task input: {path}")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(file_sha256(path)))
    return digest.hexdigest()
