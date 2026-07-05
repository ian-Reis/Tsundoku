"""Chapter discovery and ordering.

This module is format-agnostic: it locates source files in a directory,
extracts a chapter number from each filename using a configurable regex,
and returns them sorted numerically. This is the piece that fixes the
classic lexicographic bug where "chapter-10" sorts before "chapter-2".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Pattern

# Default pattern: matches "capitulo-70", "chapter_70", "cap 70", "ch70", etc.
DEFAULT_PATTERN = r"(?:cap(?:itulo)?|chapter|ch|ep(?:isodio|isode)?)[\s._-]*(\d+)"

SUPPORTED_SUFFIXES = {".pdf", ".epub", ".cbz", ".txt", ".html", ".xhtml"}


@dataclass(frozen=True)
class Chapter:
    """A single discovered chapter source file."""

    number: int
    path: Path

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()


@dataclass
class DiscoveryResult:
    """Outcome of scanning a directory for chapters."""

    chapters: list[Chapter]
    unmatched: list[Path]
    missing: list[int]
    duplicates: dict[int, list[Path]] = None  # chapter number -> extra paths

    def __post_init__(self):
        if self.duplicates is None:
            self.duplicates = {}

    @property
    def is_empty(self) -> bool:
        return not self.chapters

    @property
    def numbers(self) -> list[int]:
        return [c.number for c in self.chapters]


def compile_pattern(pattern: str = DEFAULT_PATTERN) -> Pattern[str]:
    """Compile a chapter-number regex (case-insensitive).

    The pattern must contain exactly one capturing group that captures the
    chapter number as digits.
    """
    compiled = re.compile(pattern, re.IGNORECASE)
    if compiled.groups < 1:
        raise ValueError(
            "chapter pattern must contain a capturing group for the number, "
            f"got: {pattern!r}"
        )
    return compiled


def extract_number(name: str, pattern: Pattern[str]) -> int | None:
    """Extract the chapter number from a filename, or None if absent."""
    match = pattern.search(name)
    if match is None:
        return None
    return int(match.group(1))


def _iter_source_files(
    directory: Path,
    suffixes: set[str],
    recursive: bool,
) -> Iterable[Path]:
    globber = directory.rglob("*") if recursive else directory.glob("*")
    for path in globber:
        if path.is_file() and path.suffix.lower() in suffixes:
            yield path


def discover_chapters(
    directory: str | Path,
    pattern: str = DEFAULT_PATTERN,
    suffixes: Iterable[str] | None = None,
    recursive: bool = False,
) -> DiscoveryResult:
    """Scan ``directory`` for chapter files and return them sorted.

    Parameters
    ----------
    directory:
        Folder to scan.
    pattern:
        Regex with one capture group for the chapter number.
    suffixes:
        Which file extensions to consider (default: all supported).
    recursive:
        Whether to descend into subdirectories.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"not a directory: {directory}")

    if suffixes is None:
        suffix_set = set(SUPPORTED_SUFFIXES)
    else:
        suffix_set = {s.lower() if s.startswith(".") else f".{s.lower()}" for s in suffixes}

    compiled = compile_pattern(pattern)

    chapters: list[Chapter] = []
    unmatched: list[Path] = []

    for path in _iter_source_files(directory, suffix_set, recursive):
        number = extract_number(path.name, compiled)
        if number is None:
            unmatched.append(path)
        else:
            chapters.append(Chapter(number=number, path=path))

    chapters.sort(key=lambda c: (c.number, c.path.name))
    unmatched.sort()

    # Detect and remove duplicate chapter numbers, keeping the first
    # (alphabetically) and recording the rest so the caller can warn.
    deduped: list[Chapter] = []
    duplicates: dict[int, list[Path]] = {}
    seen: dict[int, Chapter] = {}
    for chapter in chapters:
        if chapter.number in seen:
            duplicates.setdefault(chapter.number, []).append(chapter.path)
        else:
            seen[chapter.number] = chapter
            deduped.append(chapter)

    missing = _find_missing(sorted(c.number for c in deduped))

    return DiscoveryResult(
        chapters=deduped,
        unmatched=unmatched,
        missing=missing,
        duplicates=duplicates,
    )


def _find_missing(numbers: list[int]) -> list[int]:
    """Return gaps in an ascending sequence of chapter numbers."""
    if not numbers:
        return []
    full = set(range(numbers[0], numbers[-1] + 1))
    return sorted(full - set(numbers))
