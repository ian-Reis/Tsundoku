"""Splitting chapters into multiple volumes.

Two strategies:

* **by count** — a fixed number of chapters per volume (e.g. ``--split 20``).
* **by size** — build volumes incrementally and start a new one once the
  accumulated output would exceed a byte budget (e.g. ``--max-size 18MB``).

The by-size strategy needs to actually build output to measure it, so it is
driven by the CLI via a builder callback rather than computed up front.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .chapters import Chapter

_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([kKmMgG]?)[bB]?\s*$")
_UNIT_FACTOR = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def parse_size(text: str) -> int:
    """Parse a human size string like '18MB', '500k', '2g' into bytes."""
    match = _SIZE_RE.match(text)
    if not match:
        raise ValueError(f"invalid size: {text!r} (try e.g. 18MB, 500k, 2g)")
    number, unit = match.groups()
    return int(float(number) * _UNIT_FACTOR[unit.lower()])


@dataclass
class Volume:
    """A contiguous slice of chapters forming one output file."""

    index: int  # 1-based volume number
    chapters: list[Chapter]

    @property
    def first(self) -> int:
        return self.chapters[0].number

    @property
    def last(self) -> int:
        return self.chapters[-1].number


def split_by_count(chapters: list[Chapter], per_volume: int) -> list[Volume]:
    """Partition chapters into volumes of at most ``per_volume`` chapters."""
    if per_volume < 1:
        raise ValueError("chapters per volume must be >= 1")
    volumes = []
    for i in range(0, len(chapters), per_volume):
        chunk = chapters[i : i + per_volume]
        volumes.append(Volume(index=len(volumes) + 1, chapters=chunk))
    return volumes


def volume_output_path(base_output, volume: Volume, total: int):
    """Derive a per-volume output path from the base output path.

    ``book.epub`` with 3 volumes -> ``book-vol01.epub`` etc. A single volume
    keeps the original name (no suffix).
    """
    from pathlib import Path

    base = Path(base_output)
    if total <= 1:
        return base
    width = max(2, len(str(total)))
    stem = f"{base.stem}-vol{volume.index:0{width}d}"
    return base.with_name(f"{stem}{base.suffix}")


def volume_title(base_title: str, volume: Volume, total: int) -> str:
    """Append a volume marker to the book title when splitting."""
    if total <= 1:
        return base_title
    return f"{base_title} - Vol. {volume.index}"
