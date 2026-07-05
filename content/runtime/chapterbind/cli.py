"""Command-line interface for chapterbind."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .builders import BookMeta, build_cbz, build_epub, build_pdf
from .chapters import DEFAULT_PATTERN, discover_chapters
from .cover import resolve_cover
from .split import (
    Volume,
    parse_size,
    split_by_count,
    volume_output_path,
    volume_title,
)
from .unbind import unbind_cbz

FORMAT_SUFFIX = {"pdf": ".pdf", "epub": ".epub", "cbz": ".cbz"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chapterbind",
        description=(
            "Merge per-chapter novel/manga files into a single ordered "
            "PDF, EPUB, or CBZ. Chapters are ordered numerically by a "
            "number parsed from each filename, fixing lexicographic sort "
            "bugs (e.g. chapter-10 before chapter-2)."
        ),
        epilog="Example: chapterbind ./rezero -f epub -o rezero-arc6.epub "
        '-t "Re:Zero Arc 6" -a "Tappei Nagatsuki"',
    )
    parser.add_argument("input", type=Path, help="directory containing chapter files")
    parser.add_argument(
        "-o", "--output", type=Path,
        help="output file path (default: derived from title + format)",
    )
    parser.add_argument(
        "-f", "--format", choices=sorted(FORMAT_SUFFIX), default="pdf",
        help="output format (default: pdf)",
    )
    parser.add_argument(
        "-t", "--title", default=None,
        help="book title (default: input directory name)",
    )
    parser.add_argument("-a", "--author", default="Unknown", help="book author")
    parser.add_argument(
        "-c", "--cover", type=Path, default=None,
        help="cover image (default: auto-detect cover.jpg/capa.png in input dir)",
    )
    parser.add_argument(
        "--no-cover", action="store_true",
        help="disable cover, even if one is auto-detected",
    )
    parser.add_argument(
        "-l", "--language", default="pt-BR",
        help="language code for metadata (default: pt-BR)",
    )
    parser.add_argument(
        "-p", "--pattern", default=DEFAULT_PATTERN,
        help="regex with one capture group for the chapter number",
    )
    parser.add_argument(
        "-e", "--ext", action="append", dest="extensions", metavar="EXT",
        help="restrict to these source extensions (repeatable), e.g. -e pdf",
    )
    parser.add_argument(
        "--no-images", action="store_true",
        help="skip extracting embedded images (EPUB only)",
    )
    parser.add_argument(
        "--image-position", choices=["inline", "end"], default="inline",
        help="where images go in EPUB chapters (default: inline)",
    )
    parser.add_argument(
        "-r", "--recursive", action="store_true",
        help="scan subdirectories recursively",
    )
    parser.add_argument(
        "--split", type=int, default=None, metavar="N",
        help="split output into volumes of N chapters each",
    )
    parser.add_argument(
        "--max-size", type=str, default=None, metavar="SIZE",
        help="split output into volumes under SIZE (e.g. 18MB, 500k)",
    )
    parser.add_argument(
        "--allow-gaps", action="store_true",
        help="proceed even if chapters are missing in the sequence",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show discovery result without writing output",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="assume yes to prompts (non-interactive)",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="suppress progress output",
    )
    parser.add_argument(
        "-V", "--version", action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def build_split_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chapterbind split",
        description=(
            "Split a single CBZ into one CBZ per chapter. Chapter boundaries "
            "are detected from internal folders or filename chapter numbers, "
            "falling back to a fixed page count only if requested."
        ),
    )
    parser.add_argument("input", type=Path, help="the CBZ file to split")
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=None,
        help="directory for the split CBZs (default: <input name>_chapters)",
    )
    parser.add_argument(
        "--pages-per-chapter", type=int, default=None, metavar="N",
        help="fallback: split every N pages when no structure is detected",
    )
    parser.add_argument(
        "--name-template", default="capitulo-{number:03d}.cbz",
        help="output filename template (default: capitulo-{number:03d}.cbz)",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="suppress progress output",
    )
    return parser


def run_split(argv: list[str]) -> int:
    args = build_split_parser().parse_args(argv)

    if not args.input.is_file():
        print(f"error: not a file: {args.input}", file=sys.stderr)
        return 2
    if args.input.suffix.lower() != ".cbz":
        print(f"error: expected a .cbz file, got {args.input.suffix}", file=sys.stderr)
        return 2

    output_dir = args.output_dir or args.input.with_name(
        f"{args.input.stem}_chapters"
    )

    try:
        result = unbind_cbz(
            args.input,
            output_dir,
            pages_per_chapter=args.pages_per_chapter,
            name_template=args.name_template,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    method_desc = {
        "folders": "internal folders",
        "filenames": "filename chapter numbers",
        "fixed": "fixed page count (guessed boundaries)",
    }[result.method]

    if not args.quiet:
        print(f"Detected {len(result.chapters)} chapters via {method_desc}")
        for chapter in result.chapters:
            print(f"  Cap\u00edtulo {chapter.number}: {len(chapter.entries)} pages")
        print(f"\nWrote {len(result.chapters)} files to {output_dir}/")

    if result.method == "fixed" and not args.quiet:
        print(
            "\nNote: boundaries were guessed by page count. Check that "
            "chapters split where you expect.",
            file=sys.stderr,
        )

    return 0


def _echo(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg)


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [s/N]: ").strip().lower() in {"s", "sim", "y", "yes"}
    except (EOFError, OSError):
        # No interactive stdin available -> treat as "no".
        return False


def run(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Subcommand dispatch: "split" routes to the CBZ splitter; everything
    # else is the default merge behavior (kept for backwards compatibility).
    if argv and argv[0] == "split":
        return run_split(argv[1:])

    args = build_parser().parse_args(argv)

    if not args.input.is_dir():
        print(f"error: not a directory: {args.input}", file=sys.stderr)
        return 2

    try:
        result = discover_chapters(
            args.input,
            pattern=args.pattern,
            suffixes=args.extensions,
            recursive=args.recursive,
        )
    except (ValueError, NotADirectoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if result.is_empty:
        print("error: no chapter files matched", file=sys.stderr)
        if result.unmatched:
            print(
                f"  ({len(result.unmatched)} files found but none matched the "
                "chapter pattern)",
                file=sys.stderr,
            )
        return 1

    _echo(
        f"Found {len(result.chapters)} chapters "
        f"(#{result.numbers[0]}\u2013#{result.numbers[-1]})",
        args.quiet,
    )
    if not args.quiet:
        for chapter in result.chapters:
            print(f"  {chapter.number:>4}  {chapter.path.name}")

    if result.unmatched:
        _echo(
            f"\n{len(result.unmatched)} file(s) ignored (no chapter number):",
            args.quiet,
        )
        for path in result.unmatched:
            _echo(f"  - {path.name}", args.quiet)

    if result.duplicates:
        dup_count = sum(len(v) for v in result.duplicates.values())
        _echo(
            f"\nWARNING: {dup_count} duplicate chapter file(s) skipped "
            f"(numbers: {sorted(result.duplicates)}):",
            args.quiet,
        )
        for number, paths in sorted(result.duplicates.items()):
            for path in paths:
                _echo(f"  - #{number}: {path.name}", args.quiet)

    if result.missing:
        _echo(f"\nWARNING: missing chapters in sequence: {result.missing}", args.quiet)
        if not args.allow_gaps and not args.dry_run:
            if not _confirm("Continue despite gaps?", args.yes):
                print("aborted.", file=sys.stderr)
                return 1

    if args.dry_run:
        _echo("\n(dry run - nothing written)", args.quiet)
        return 0

    if args.split is not None and args.max_size is not None:
        print("error: use only one of --split or --max-size", file=sys.stderr)
        return 2

    max_bytes = None
    if args.max_size is not None:
        try:
            max_bytes = parse_size(args.max_size)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    base_title = args.title or args.input.resolve().name
    base_output = args.output or Path(
        f"{_slugify(base_title)}{FORMAT_SUFFIX[args.format]}"
    )

    # Resolve cover: explicit path, else auto-detect, unless disabled.
    cover = None
    if not args.no_cover:
        try:
            cover = resolve_cover(args.input, args.cover)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if cover is not None:
            _echo(f"Using cover: {cover.name}", args.quiet)

    # Determine volumes.
    if args.split is not None:
        try:
            volumes = split_by_count(result.chapters, args.split)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    elif max_bytes is not None:
        volumes = _split_by_size(
            result.chapters, base_title, base_output, cover, args, max_bytes
        )
    else:
        volumes = [Volume(index=1, chapters=result.chapters)]

    total = len(volumes)
    if total > 1:
        _echo(f"\nSplitting into {total} volumes", args.quiet)

    unit = {"pdf": "pages", "epub": "chapters", "cbz": "images"}[args.format]
    for volume in volumes:
        out = volume_output_path(base_output, volume, total)
        vtitle = volume_title(base_title, volume, total)
        meta = BookMeta(title=vtitle, author=args.author, language=args.language)
        label = f" (caps {volume.first}-{volume.last})" if total > 1 else ""
        _echo(f"Building {args.format.upper()} -> {out}{label}", args.quiet)
        count = _build_one(args, volume.chapters, meta, out, cover)
        size_mb = out.stat().st_size / (1024 * 1024)
        _echo(f"  Done: {out.name} ({count} {unit}, {size_mb:.1f} MB)", args.quiet)

    return 0


def _build_one(args, chapters, meta, output, cover):
    """Build a single output file with the chosen format."""
    if args.format == "epub":
        return build_epub(
            chapters, meta, output, cover,
            with_images=not args.no_images,
            image_position=args.image_position,
        )
    if args.format == "pdf":
        return build_pdf(chapters, meta, output, cover)
    return build_cbz(chapters, meta, output, cover)


def _split_by_size(chapters, base_title, base_output, cover, args, max_bytes):
    """Greedily pack chapters into volumes under ``max_bytes``.

    Builds a temporary file per trial to measure real output size. A volume
    always contains at least one chapter, even if that chapter alone exceeds
    the budget (a warning is printed in that case).
    """
    import tempfile

    volumes = []
    current: list = []
    tmpdir = Path(tempfile.mkdtemp(prefix="chapterbind-"))

    def trial_size(chs):
        meta = BookMeta(title=base_title, author=args.author, language=args.language)
        trial = tmpdir / f"trial{base_output.suffix}"
        _build_one(args, chs, meta, trial, cover)
        return trial.stat().st_size

    for chapter in chapters:
        candidate = current + [chapter]
        if current and trial_size(candidate) > max_bytes:
            # Close current volume, start new one with this chapter.
            volumes.append(Volume(index=len(volumes) + 1, chapters=current))
            current = [chapter]
        else:
            current = candidate

    if current:
        # Warn if the last (or any single-chapter) volume is over budget.
        if len(current) == 1 and trial_size(current) > max_bytes:
            print(
                f"warning: chapter {current[0].number} alone exceeds the size "
                "limit; keeping it as its own volume",
                file=sys.stderr,
            )
        volumes.append(Volume(index=len(volumes) + 1, chapters=current))

    import shutil

    shutil.rmtree(tmpdir, ignore_errors=True)
    return volumes


def _slugify(text: str) -> str:
    import re

    slug = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", slug) or "output"


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
