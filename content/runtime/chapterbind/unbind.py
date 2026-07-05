"""Splitting a single CBZ into per-chapter CBZ files.

Detects chapter boundaries as faithfully as possible, trying the most
reliable signals first:

1. **Internal folders** — images grouped in subdirectories (``Cap 01/``,
   ``Chapter 2/``) → one chapter per folder.
2. **Filename chapter numbers** — a chapter number embedded in each image
   name (``c001_p01``, ``ch2_page3``) → group by that number.
3. **Fixed page count** — fallback only; splits every ``pages_per_chapter``
   images, with a warning that the boundary is a guess.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# Chapter number embedded in an image filename.
_NAME_CHAPTER_RE = re.compile(
    r"(?:c|ch|cap|capitulo|chapter)[\s._-]*(\d+)", re.IGNORECASE
)
# Chapter number in a folder name.
_DIR_CHAPTER_RE = re.compile(
    r"(?:c|ch|cap|capitulo|chapter|vol|volume)?[\s._-]*(\d+)", re.IGNORECASE
)


@dataclass
class DetectedChapter:
    """A group of image entries forming one chapter inside the CBZ."""

    number: int
    entries: list[str]  # zip entry names, in page order


@dataclass
class UnbindResult:
    chapters: list[DetectedChapter]
    method: str  # "folders", "filenames", or "fixed"


def _image_entries(zf: zipfile.ZipFile) -> list[str]:
    return sorted(
        n for n in zf.namelist()
        if PurePosixPath(n).suffix.lower() in _IMAGE_SUFFIXES
    )


def _by_folders(entries: list[str]) -> list[DetectedChapter] | None:
    """Group entries by their top-level folder, if that partitions them."""
    groups: dict[str, list[str]] = {}
    for entry in entries:
        parts = PurePosixPath(entry).parts
        if len(parts) < 2:
            return None  # a top-level file → not a clean folder layout
        groups.setdefault(parts[0], []).append(entry)

    if len(groups) < 2:
        return None  # everything in one folder → no chapter split

    chapters = []
    for idx, (folder, items) in enumerate(sorted(groups.items()), start=1):
        m = _DIR_CHAPTER_RE.search(folder)
        number = int(m.group(1)) if m else idx
        chapters.append(DetectedChapter(number=number, entries=sorted(items)))

    chapters.sort(key=lambda c: c.number)
    return chapters


def _by_filenames(entries: list[str]) -> list[DetectedChapter] | None:
    """Group entries by a chapter number parsed from each filename."""
    groups: dict[int, list[str]] = {}
    for entry in entries:
        stem = PurePosixPath(entry).name
        m = _NAME_CHAPTER_RE.search(stem)
        if m is None:
            return None  # any file without a number → unreliable
        groups.setdefault(int(m.group(1)), []).append(entry)

    if len(groups) < 2:
        return None

    return [
        DetectedChapter(number=num, entries=sorted(items))
        for num, items in sorted(groups.items())
    ]


def _by_fixed(entries: list[str], per_chapter: int) -> list[DetectedChapter]:
    chapters = []
    for i in range(0, len(entries), per_chapter):
        chunk = entries[i : i + per_chapter]
        chapters.append(
            DetectedChapter(number=len(chapters) + 1, entries=chunk)
        )
    return chapters


def detect_chapters(
    cbz_path: str | Path,
    pages_per_chapter: int | None = None,
) -> UnbindResult:
    """Detect chapter boundaries inside a single CBZ.

    Tries folders, then filenames, then (if ``pages_per_chapter`` is given)
    a fixed-count fallback. Raises ``ValueError`` if nothing works.
    """
    cbz_path = Path(cbz_path)
    with zipfile.ZipFile(cbz_path) as zf:
        entries = _image_entries(zf)

    if not entries:
        raise ValueError(f"no images found in {cbz_path.name}")

    chapters = _by_folders(entries)
    if chapters is not None:
        return UnbindResult(chapters=chapters, method="folders")

    chapters = _by_filenames(entries)
    if chapters is not None:
        return UnbindResult(chapters=chapters, method="filenames")

    if pages_per_chapter:
        chapters = _by_fixed(entries, pages_per_chapter)
        return UnbindResult(chapters=chapters, method="fixed")

    raise ValueError(
        "could not detect chapter boundaries (no folders or filename "
        "numbers); pass --pages-per-chapter N to split by fixed count"
    )


def unbind_cbz(
    cbz_path: str | Path,
    output_dir: str | Path,
    pages_per_chapter: int | None = None,
    name_template: str = "capitulo-{number:03d}.cbz",
) -> UnbindResult:
    """Split a CBZ into one CBZ per detected chapter.

    Writes files into ``output_dir`` using ``name_template`` and returns the
    detection result (so callers can report the method and counts).
    """
    cbz_path = Path(cbz_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = detect_chapters(cbz_path, pages_per_chapter)

    with zipfile.ZipFile(cbz_path) as src:
        for chapter in result.chapters:
            out_name = name_template.format(number=chapter.number)
            out_path = output_dir / out_name
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as dst:
                for idx, entry in enumerate(chapter.entries):
                    ext = PurePosixPath(entry).suffix.lower()
                    arcname = f"p{idx:04d}{ext}"
                    dst.writestr(arcname, src.read(entry))

    return result
