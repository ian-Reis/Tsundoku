"""Cover image discovery and helpers.

Supports an explicit cover path or auto-detection of a ``cover.*`` image
in the input directory. Used by all three builders.
"""

from __future__ import annotations

from pathlib import Path

# Extensions we accept as cover images, in detection-priority order.
COVER_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")

# Basenames auto-detected when no explicit cover is given, in priority order.
COVER_STEMS = ("cover", "capa", "folder")

MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def media_type_for(path: Path) -> str:
    """Return the MIME type for a cover image path."""
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def resolve_cover(
    directory: Path,
    explicit: str | Path | None = None,
) -> Path | None:
    """Resolve which cover image to use.

    If ``explicit`` is given it must exist (raises ``FileNotFoundError``
    otherwise). If not given, auto-detect ``cover.*``/``capa.*`` in
    ``directory``. Returns ``None`` when no cover is found.
    """
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(f"cover image not found: {path}")
        return path

    for stem in COVER_STEMS:
        for suffix in COVER_SUFFIXES:
            candidate = directory / f"{stem}{suffix}"
            if candidate.is_file():
                return candidate
    return None
