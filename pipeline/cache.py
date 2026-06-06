"""Content-hash cache so unchanged sources can skip the expensive extract step.

Stores a flat ``{key: sha256hex}`` map on disk. The pipeline asks
:meth:`HashCache.changed` whether a freshly-fetched payload differs from the
last run; if not, extraction (and any LLM call) is skipped.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Default cache location: <repo root>/pipeline/cache/hashes.json
_DEFAULT_PATH = Path(__file__).resolve().parent / "cache" / "hashes.json"


def sha256_hex(content) -> str:
    """Return the hex SHA-256 of ``content`` (accepts ``str`` or ``bytes``)."""
    if isinstance(content, str):
        data = content.encode("utf-8")
    elif isinstance(content, (bytes, bytearray)):
        data = bytes(content)
    else:
        # Fall back to a stable string representation for other types.
        data = str(content).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class HashCache:
    """A tiny persistent ``{key: hash}`` store for change detection."""

    def __init__(self, path: str | Path = _DEFAULT_PATH) -> None:
        self.path = Path(path)
        self._map: dict[str, str] = {}
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self._map = {str(k): str(v) for k, v in loaded.items()}
            except (json.JSONDecodeError, OSError):
                # Corrupt/unreadable cache: start fresh rather than crash.
                self._map = {}

    def changed(self, key: str, content) -> bool:
        """Return True if ``content`` is new or differs from the stored hash.

        Always updates the in-memory map to the new hash (so :meth:`save`
        persists it). Returns True for a never-seen key.
        """
        new_hash = sha256_hex(content)
        old_hash = self._map.get(key)
        self._map[key] = new_hash
        return old_hash != new_hash

    def save(self) -> None:
        """Persist the current map to disk, creating the cache dir if needed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as fh:
            json.dump(self._map, fh, indent=2, sort_keys=True)
            fh.write("\n")
