#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from aio_config import CANONICAL_HID_MARKER, SUPPORTED_HID_MARKERS, read_hid_marker, write_hid_marker


def _looks_like_series_folder(path: Path) -> bool:
    if not path.is_dir():
        return False
    for child in path.iterdir():
        if child.name.startswith(".") and child.name not in SUPPORTED_HID_MARKERS:
            continue
        if child.is_file() and child.suffix.lower() in {".pdf", ".epub", ".cbz"}:
            return True
        if child.is_dir() and child.name == "images":
            return True
    return read_hid_marker(path) is not None


def _merge_tree(src: Path, dst: Path, *, dry_run: bool) -> tuple[int, int]:
    copied = 0
    skipped = 0
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        target_root = dst / rel
        if not dry_run:
            target_root.mkdir(parents=True, exist_ok=True)
        for filename in files:
            src_file = Path(root) / filename
            dst_file = target_root / filename
            if dst_file.exists():
                skipped += 1
                continue
            copied += 1
            if not dry_run:
                shutil.copy2(src_file, dst_file)
        for dirname in dirs:
            if not dry_run:
                (target_root / dirname).mkdir(exist_ok=True)
    return copied, skipped


def migrate_library(source_dir: Path, target_dir: Path, *, dry_run: bool, remove_old_markers: bool) -> list[str]:
    report: list[str] = []
    if not source_dir.exists():
        return [f"Source does not exist: {source_dir}"]
    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)

    for entry in sorted(source_dir.iterdir(), key=lambda p: p.name.casefold()):
        if not _looks_like_series_folder(entry):
            continue
        target = target_dir / entry.name
        hid = read_hid_marker(entry)
        if target.exists():
            copied, skipped = _merge_tree(entry, target, dry_run=dry_run)
            action = f"merge {entry.name}: {copied} file(s), {skipped} existing"
        else:
            action = f"move {entry.name}"
            if not dry_run:
                shutil.move(str(entry), str(target))
        if hid and not dry_run:
            write_hid_marker(target, hid)
        if remove_old_markers and not dry_run:
            for marker in SUPPORTED_HID_MARKERS:
                if marker == CANONICAL_HID_MARKER:
                    continue
                try:
                    (target / marker).unlink()
                except FileNotFoundError:
                    pass
        report.append(action)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate a legacy AIO library into the canonical manga/ root.")
    parser.add_argument("--source-dir", default="comics")
    parser.add_argument("--target-dir", default="manga")
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--execute", action="store_true", help="Perform the migration. Without this, runs as a dry-run.")
    parser.add_argument("--remove-old-markers", action="store_true")
    args = parser.parse_args()

    dry_run = args.dry_run or not args.execute
    report = migrate_library(
        Path(args.source_dir),
        Path(args.target_dir),
        dry_run=dry_run,
        remove_old_markers=args.remove_old_markers,
    )
    mode = "DRY RUN" if dry_run else "EXECUTE"
    print(f"{mode}: {args.source_dir} -> {args.target_dir}")
    if not report:
        print("No series folders found.")
    for line in report:
        print(f"- {line}")


if __name__ == "__main__":
    main()
