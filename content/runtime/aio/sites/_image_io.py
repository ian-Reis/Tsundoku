"""Image-format sniffing + atomic-finalize helpers, shared between aio-dl.py
and per-site handlers that implement their own image fetcher.

What this module owns:
  - Magic-byte detection for JPEG / PNG / GIF / WebP / AVIF / HEIC.
  - Content-Type fallback when magic is ambiguous.
  - `finalize_pending_image()`: atomic-rename a `.pending_<base>` tempfile to
    `<folder>/<base><ext>` once bytes have landed.

What reads from it:
  - `aio-dl.py:dl_image` (the main download path) — uses both helpers.
  - `aio-dl.py:_start_image_prefetch._worker` and Phase 1/2 binary classification.
  - `sites/mangafire.py:fast_download_images` (curl_cffi async path) — uses
    `finalize_pending_image` after each successful page fetch.

Why a separate module: `aio-dl.py` is at the top of the import graph (it
imports from `sites/`); `sites/mangafire.py` cannot import from `aio-dl.py`
without a circular dep. Pulling the helpers out into a leaf module is the
minimum-blast-radius refactor.

Originally lived in aio-dl.py at lines 808-881 (Phase A, 2026-05-07). Module
extracted 2026-05-09 to share with MangaFire's fast download path.
"""
from __future__ import annotations

import os
from typing import Optional

# Magic-byte prefixes. Hex-readable comments inline.
JPEG_MAGIC = b"\xff\xd8"           # SOI marker
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"   # ISO 15948 §5.2 file signature
GIF_MAGIC = b"GIF8"                # both GIF87a and GIF89a
# WebP/AVIF/HEIC use ISO BMFF / RIFF containers — checked via byte ranges.


def content_type_to_ext(content_type: str) -> Optional[str]:
    """Map an `image/*` Content-Type to a file extension. Returns None for
    unrecognized types so the caller falls back to a default. The mapping
    intentionally normalizes `image/jpg` → `.jpg` even though it's not the
    IANA-registered name (some CDNs send it)."""
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/heic": ".heic",
        "image/heif": ".heic",
        "image/gif": ".gif",
    }.get((content_type or "").strip().lower())


def sniff_image_extension(head: bytes, content_type: Optional[str] = None) -> str:
    """Return the most accurate file extension (with leading dot) for an image
    given its first ≥12 bytes and an optional Content-Type. Magic bytes are
    primary; Content-Type is consulted only when magic is ambiguous. Falls
    back to `.jpg` so callers always get a usable extension (matches prior
    blanket-`.jpg` behavior for unknown content)."""
    if head:
        if head.startswith(JPEG_MAGIC):
            return ".jpg"
        if head.startswith(PNG_MAGIC):
            return ".png"
        if head.startswith(GIF_MAGIC):
            return ".gif"
        # WebP: bytes 0-3 = 'RIFF', bytes 8-11 = 'WEBP'.
        if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return ".webp"
        # AVIF/HEIC: ISO-BMFF "ftyp" box. Major brand at offset 8-11 tells
        # us the codec family. We only special-case AVIF; HEIC is rare in
        # manga aggregators but recognized so we don't accidentally label
        # it `.jpg`.
        if len(head) >= 12 and head[4:8] == b"ftyp":
            major = head[8:12]
            if major in (b"avif", b"avis"):
                return ".avif"
            if major in (b"heic", b"heix", b"mif1", b"msf1"):
                return ".heic"
    fallback = content_type_to_ext(
        (content_type or "").split(";", 1)[0]
    )
    return fallback or ".jpg"


def finalize_pending_image(
    pending_path: str, folder: str, base: str, content_type: Optional[str]
) -> Optional[str]:
    """Sniff a successfully-downloaded pending file's first bytes, atomic-
    rename it to `<folder>/<base><ext>`, and return the final path. Returns
    None if the pending file is missing (caller should treat as failure).
    `os.replace` is atomic on both POSIX and NT when source/dest share a
    volume — pending and final live in the same folder, so this is safe."""
    if not os.path.exists(pending_path):
        return None
    try:
        with open(pending_path, "rb") as fh:
            head = fh.read(32)
    except Exception:
        head = b""
    ext = sniff_image_extension(head, content_type)
    final_path = os.path.join(folder, base + ext)
    os.replace(pending_path, final_path)
    return final_path
