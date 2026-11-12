"""Hash the canonical COPY payload before any Docker build step mutates /app.

This is a content identity, not Docker's tar-stream digest: tar header timestamps
and archive order are deliberately excluded. It covers paths, types, modes,
symlink targets and the bytes of every regular file actually copied to /app.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import sys


def copied_tree_sha256(root: Path) -> str:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("COPY payload root must be a directory")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: os.fsencode(item.relative_to(root))):
        relative = os.fsencode(path.relative_to(root))
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISREG(info.st_mode):
            with path.open("rb") as handle:
                payload = hashlib.file_digest(handle, "sha256").digest()
            kind = b"file"
        elif stat.S_ISLNK(info.st_mode):
            payload = os.fsencode(os.readlink(path))
            kind = b"link"
        elif stat.S_ISDIR(info.st_mode):
            payload = b""
            kind = b"directory"
        else:
            raise ValueError(f"unsupported entry in COPY payload: {path}")
        for field in (relative, kind, str(mode).encode("ascii"), payload):
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)
    return digest.hexdigest()


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: context_identity.py COPY_ROOT OUTSIDE_ROOT_DIGEST_PATH")
    root, output = Path(sys.argv[1]).resolve(strict=True), Path(sys.argv[2]).resolve()
    if output == root or output.is_relative_to(root):
        raise SystemExit("digest output must be outside the copied source tree")
    value = copied_tree_sha256(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(value + "\n", encoding="ascii")
    print("COPY payload sha256=" + value)


if __name__ == "__main__":
    main()
