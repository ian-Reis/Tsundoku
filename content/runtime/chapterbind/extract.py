"""Content extraction from chapter source files.

Extracts a chapter into an ordered sequence of *content blocks* — text
paragraphs and images — so the EPUB builder can render both. PDF text
extraction is best-effort: web-novel PDFs usually have a selectable text
layer; scanned PDFs yield little (OCR is out of scope). Embedded images
(light-novel illustrations) are extracted page by page.
"""

from __future__ import annotations

import hashlib
import html
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TextBlock:
    """A run of text (one or more paragraphs)."""

    text: str


@dataclass
class ImageBlock:
    """An embedded image extracted from a source file."""

    data: bytes
    suffix: str  # e.g. ".jpg", ".png"

    @property
    def digest(self) -> str:
        """Short content hash, used to build stable filenames."""
        return hashlib.sha1(self.data).hexdigest()[:12]


@dataclass
class ChapterContent:
    """Ordered blocks (text + images) extracted from one chapter."""

    blocks: list = field(default_factory=list)

    @property
    def text_blocks(self) -> list[TextBlock]:
        return [b for b in self.blocks if isinstance(b, TextBlock)]

    @property
    def images(self) -> list[ImageBlock]:
        return [b for b in self.blocks if isinstance(b, ImageBlock)]

    @property
    def has_images(self) -> bool:
        return any(isinstance(b, ImageBlock) for b in self.blocks)


# --------------------------------------------------------------------------- #
# Backwards-compatible text-only helper
# --------------------------------------------------------------------------- #
def extract_text(path: Path) -> str:
    """Extract plain text only (no images). Kept for compatibility."""
    content = extract_content(path, with_images=False)
    return "\n\n".join(b.text for b in content.text_blocks).strip()


# --------------------------------------------------------------------------- #
# Structured extraction
# --------------------------------------------------------------------------- #
def extract_content(path: Path, with_images: bool = True) -> ChapterContent:
    """Extract text and (optionally) images as ordered content blocks."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path, with_images)
    if suffix == ".txt":
        text = path.read_text(encoding="utf-8", errors="replace")
        return ChapterContent(blocks=[TextBlock(text=text.strip())])
    if suffix in {".html", ".xhtml"}:
        text = _strip_html(path.read_text(encoding="utf-8", errors="replace"))
        return ChapterContent(blocks=[TextBlock(text=text)])
    if suffix == ".epub":
        return _extract_epub(path)
    raise ValueError(f"cannot extract content from {suffix} files")


def _extract_pdf(path: Path, with_images: bool) -> ChapterContent:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    content = ChapterContent()

    # Dedup por hash de conteúdo, como rede de segurança caso uma imagem seja
    # desenhada em mais de uma página.
    seen_images: set[str] = set()

    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            content.blocks.append(TextBlock(text=text))

        if not with_images:
            continue

        # Só as imagens EFETIVAMENTE desenhadas nesta página (operador Do). Nos
        # PDFs do centralnovel as ilustrações ficam em páginas próprias (sem
        # texto), então emiti-las na página onde são desenhadas já as coloca na
        # ordem de leitura correta. (page.images não serve: lista o XObject em
        # TODAS as páginas, mesmo onde não é desenhado — causava imagem na página
        # errada e repetição.)
        for img in _drawn_images(page, reader):
            data, suffix = _normalize_image(img)
            if data is None:
                continue
            block = ImageBlock(data=data, suffix=suffix)
            if block.digest in seen_images:
                continue
            seen_images.add(block.digest)
            content.blocks.append(block)

    return content


def _drawn_images(page, reader) -> list:
    """Imagens realmente desenhadas na página, na ordem de desenho, lendo os
    operadores `Do` do content stream. Mapeia o nome do XObject (ex.: /I1) de
    volta ao objeto de imagem que o pypdf já decodifica em page.images."""
    from pypdf.generic import ContentStream

    try:
        by_name = {}
        for im in page.images:
            # pypdf nomeia como "<chave>.<ext>" (ex.: "I1.jpg") e pode aninhar
            # ("Fm0/I1.jpg"); o operador Do usa só a chave ("/I1"). Normaliza pro
            # último componente sem extensão.
            raw = (getattr(im, "name", "") or "").split("/")[-1]
            key = raw.rsplit(".", 1)[0]
            by_name[key] = im
    except Exception:
        return []
    if not by_name:
        return []

    try:
        stream = ContentStream(page.get_contents(), reader)
    except Exception:
        # Sem parsear o stream, cai pro comportamento antigo (todas as imagens).
        return list(by_name.values())

    drawn = []
    for operands, op in stream.operations:
        if op == b"Do" and operands:
            img = by_name.get(str(operands[0]).lstrip("/"))
            if img is not None:
                drawn.append(img)
    return drawn


def _normalize_image(img) -> tuple[bytes | None, str]:
    """Return (bytes, suffix) for a pypdf image object, or (None, '')."""
    name = getattr(img, "name", "") or ""
    suffix = Path(name).suffix.lower()
    data = getattr(img, "data", None)

    if data is None:
        return None, ""

    if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        # pypdf usually decodes to PNG/JPEG; default to PNG when unknown.
        suffix = ".png"

    # Skip trivially small images (icons, artifacts).
    if len(data) < 1024:
        return None, ""

    return data, suffix


def _extract_epub(path: Path) -> ChapterContent:
    content = ChapterContent()
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.lower().endswith((".html", ".xhtml", ".htm")):
                raw = zf.read(name).decode("utf-8", errors="replace")
                text = _strip_html(raw)
                if text:
                    content.blocks.append(TextBlock(text=text))
    return content


def _strip_html(raw: str) -> str:
    """Very small HTML-to-text: drop tags, unescape entities."""
    import re

    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"</(p|div|br|h[1-6])\s*>", "\n\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()
