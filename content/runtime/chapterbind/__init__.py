"""chapterbind - merge per-chapter novel/manga files into one ordered book.

Public API:
    discover_chapters, Chapter, DiscoveryResult  -- from .chapters
    BookMeta, build_pdf, build_epub, build_cbz   -- from .builders
"""

from __future__ import annotations

__version__ = "1.4.0"

from .builders import BookMeta, build_cbz, build_epub, build_pdf
from .chapters import Chapter, DiscoveryResult, discover_chapters

__all__ = [
    "__version__",
    "BookMeta",
    "build_pdf",
    "build_epub",
    "build_cbz",
    "Chapter",
    "DiscoveryResult",
    "discover_chapters",
]
