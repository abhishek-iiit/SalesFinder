"""The code-version fingerprint: a hash of the installed package's source (``*.py``) and prompt
and playbook (``*.md``) files. The worker stores it on its ``runs`` row and the web app compares
it with its own, so running different code in the two processes (one restarted after an upgrade,
the other not) shows up as a banner instead of relying on the deploy note."""

import hashlib
from functools import lru_cache
from pathlib import Path

import salesfinder

_PATTERNS = ("*.py", "*.md")
_UNREADABLE = b"\x00<unreadable>\x00"


def fingerprint(root: Path) -> str:
    """sha256 over the sorted (relative path, bytes) of every ``*.py`` and ``*.md`` file under
    ``root``, first 12 hex characters. Each part is length-prefixed, so moving bytes between a
    path and its content (or between files) always changes the result. An unreadable file
    hashes as a fixed marker instead of raising."""
    files = sorted(
        (path.relative_to(root).as_posix(), path)
        for pattern in _PATTERNS
        for path in root.rglob(pattern)
        if path.is_file()
    )
    digest = hashlib.sha256()
    for rel, path in files:
        try:
            content = path.read_bytes()
        except OSError:
            # Never fail a process start over one unreadable file; it still changes the result.
            content = _UNREADABLE
        for part in (rel.encode("utf-8"), content):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()[:12]


@lru_cache(maxsize=1)
def code_version() -> str:
    """This process's fingerprint, computed once (at the code it was started with)."""
    return fingerprint(Path(salesfinder.__file__).resolve().parent)
