"""Output builders: PDF, EPUB, and CBZ.

Each builder takes the ordered list of chapters plus book metadata and
writes a single consolidated file. The PDF and CBZ builders operate on the
source files directly (page/image passthrough). The EPUB builder extracts
text so the result is reflowable — ideal for e-ink novel reading.
"""

from __future__ import annotations

import html
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .chapters import Chapter
from .cover import media_type_for


@dataclass
class BookMeta:
    """Metadata applied to the consolidated output."""

    title: str
    author: str = "Unknown"
    language: str = "pt-BR"
    identifier: str = field(default_factory=lambda: f"urn:uuid:{uuid.uuid4()}")


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def build_pdf(
    chapters: list[Chapter],
    meta: BookMeta,
    output: Path,
    cover: Path | None = None,
) -> int:
    """Merge chapter PDFs into one, with per-chapter bookmarks.

    Returns the total page count. Non-PDF sources are skipped with no
    attempt at conversion (use the EPUB builder for text sources). If
    ``cover`` is given, it is rendered as the first page.
    """
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()

    if cover is not None:
        _add_pdf_cover_page(writer, cover)

    for chapter in chapters:
        if chapter.suffix != ".pdf":
            continue
        reader = PdfReader(str(chapter.path))
        start = len(writer.pages)
        for page in reader.pages:
            writer.add_page(page)
        writer.add_outline_item(f"Cap\u00edtulo {chapter.number}", start)

    writer.add_metadata({"/Title": meta.title, "/Author": meta.author})

    # Deduplica objetos byte-idênticos entre capítulos. Como cada capítulo é um
    # PdfReader separado, o pypdf reembute a MESMA imagem uma vez por arquivo
    # (fundos/marcas recorrentes), inflando o PDF com cópias. compress_identical_
    # objects() colapsa as cópias idênticas numa só. (no-op se o pypdf for antigo)
    try:
        writer.compress_identical_objects()
    except AttributeError:
        pass

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "wb") as fh:
        writer.write(fh)
    return len(writer.pages)


def _add_pdf_cover_page(writer, cover: Path) -> None:
    """Render ``cover`` as the first page of the PDF.

    Uses reportlab if available for a proper full-page image; otherwise
    falls back to embedding the raw image via Pillow. If neither is
    installed, the cover is silently skipped so the merge still succeeds.
    """
    try:
        import io

        from PIL import Image
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
    except ImportError:
        return

    from pypdf import PdfReader

    with Image.open(cover) as img:
        width, height = img.size

    buffer = io.BytesIO()
    pdf_canvas = canvas.Canvas(buffer, pagesize=(width, height))
    pdf_canvas.drawImage(
        ImageReader(str(cover)), 0, 0, width=width, height=height
    )
    pdf_canvas.showPage()
    pdf_canvas.save()
    buffer.seek(0)

    cover_reader = PdfReader(buffer)
    writer.add_page(cover_reader.pages[0])
    writer.add_outline_item("Capa", 0)


# --------------------------------------------------------------------------- #
# CBZ
# --------------------------------------------------------------------------- #
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def build_cbz(
    chapters: list[Chapter],
    meta: BookMeta,
    output: Path,
    cover: Path | None = None,
) -> int:
    """Merge chapter CBZ archives into one, preserving reading order.

    Images from each chapter are renamed with a zero-padded prefix so the
    global page order stays correct across chapters. Returns image count.
    If ``cover`` is given, it becomes the first image (page 0).
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as out:
        if cover is not None:
            # "0000_" prefix guarantees it sorts before any chapter.
            out.writestr(f"0000_cover{cover.suffix.lower()}", cover.read_bytes())
            count += 1
        for chapter in chapters:
            if chapter.suffix != ".cbz":
                continue
            with zipfile.ZipFile(chapter.path) as src:
                names = sorted(
                    n for n in src.namelist()
                    if Path(n).suffix.lower() in _IMAGE_SUFFIXES
                )
                for idx, name in enumerate(names):
                    ext = Path(name).suffix.lower()
                    arcname = f"c{chapter.number:04d}_p{idx:04d}{ext}"
                    out.writestr(arcname, src.read(name))
                    count += 1
        # ComicInfo.xml for readers that support it.
        out.writestr("ComicInfo.xml", _comic_info_xml(meta))
    return count


def _comic_info_xml(meta: BookMeta) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        f"  <Title>{html.escape(meta.title)}</Title>\n"
        f"  <Writer>{html.escape(meta.author)}</Writer>\n"
        f"  <LanguageISO>{html.escape(meta.language)}</LanguageISO>\n"
        "</ComicInfo>\n"
    )


# --------------------------------------------------------------------------- #
# EPUB
# --------------------------------------------------------------------------- #
def build_epub(
    chapters: list[Chapter],
    meta: BookMeta,
    output: Path,
    cover: Path | None = None,
    with_images: bool = True,
    image_position: str = "inline",
) -> int:
    """Build a reflowable EPUB from each chapter's text and images.

    Returns the number of chapters written. Produces a minimal, valid
    EPUB 3 with a navigation document, so chapter jumps work on-device.
    If ``cover`` is given, it is embedded as the EPUB cover image and shown
    as the first page.

    Parameters
    ----------
    with_images:
        Whether to extract and embed images from the source files.
    image_position:
        ``"inline"`` keeps images in their original position among the
        text; ``"end"`` collects them at the end of each chapter.
    """
    from .extract import extract_content

    output.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    manifest_items = []
    spine_items = []
    nav_points = []
    cover_meta = ""
    seen_media: set[str] = set()

    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as epub:
        # mimetype must be first and stored (uncompressed).
        epub.writestr("mimetype", "application/epub+zip")

        epub.writestr("META-INF/container.xml", _CONTAINER_XML)

        if cover is not None:
            cover_href = f"images/cover{cover.suffix.lower()}"
            media = media_type_for(cover)
            epub.writestr(f"OEBPS/{cover_href}", cover.read_bytes())
            manifest_items.append(
                f'<item id="cover-image" href="{cover_href}" '
                f'media-type="{media}" properties="cover-image"/>'
            )
            # An XHTML page that displays the cover, placed first in spine.
            epub.writestr("OEBPS/text/cover.xhtml", _cover_xhtml(cover_href))
            manifest_items.append(
                '<item id="cover" href="text/cover.xhtml" '
                'media-type="application/xhtml+xml"/>'
            )
            spine_items.append('<itemref idref="cover" linear="yes"/>')
            cover_meta = '\n    <meta name="cover" content="cover-image"/>'

        for chapter in chapters:
            try:
                content = extract_content(chapter.path, with_images=with_images)
            except ValueError:
                continue

            cid = f"chap{chapter.number:04d}"
            href = f"text/{cid}.xhtml"
            title = f"Cap\u00edtulo {chapter.number}"

            # Write image files and build a list of their in-chapter hrefs.
            image_refs = []  # list of (block, relative_href_from_xhtml)
            for idx, image in enumerate(content.images):
                img_name = f"images/{cid}_{idx:03d}_{image.digest}{image.suffix}"
                img_path = f"OEBPS/{img_name}"
                if img_name not in seen_media:
                    epub.writestr(img_path, image.data)
                    media = _IMAGE_MEDIA_TYPES.get(image.suffix, "image/jpeg")
                    manifest_items.append(
                        f'<item id="img_{cid}_{idx:03d}" href="{img_name}" '
                        f'media-type="{media}"/>'
                    )
                    seen_media.add(img_name)
                # href relative to the xhtml file (which lives in text/).
                image_refs.append((image, f"../{img_name}"))

            xhtml = _chapter_xhtml_blocks(
                title, content.blocks, image_refs, image_position
            )
            epub.writestr(f"OEBPS/{href}", xhtml)
            manifest_items.append(
                f'<item id="{cid}" href="{href}" media-type="application/xhtml+xml"/>'
            )
            spine_items.append(f'<itemref idref="{cid}"/>')
            nav_points.append(f'<li><a href="{href}">{html.escape(title)}</a></li>')
            written += 1

        epub.writestr("OEBPS/nav.xhtml", _nav_xhtml(meta, nav_points))
        manifest_items.append(
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
            'properties="nav"/>'
        )
        epub.writestr(
            "OEBPS/content.opf",
            _content_opf(meta, manifest_items, spine_items, cover_meta),
        )

    return written


def _cover_xhtml(cover_href: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="pt-BR">\n'
        "  <head>\n"
        "    <title>Capa</title>\n"
        "    <style>\n"
        "      body { margin: 0; padding: 0; text-align: center; }\n"
        "      img { max-width: 100%; max-height: 100vh; }\n"
        "    </style>\n"
        "  </head>\n"
        "  <body>\n"
        f'    <img src="../{cover_href}" alt="Capa"/>\n'
        "  </body>\n"
        "</html>\n"
    )


_CONTAINER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    '  <rootfiles>\n'
    '    <rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/>\n'
    '  </rootfiles>\n'
    '</container>\n'
)


_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _reflow_paragraphs(text: str) -> list[str]:
    """Reassemble hard-wrapped lines into logical paragraphs.

    Text extracted from PDFs has a newline at every visual line break, which
    would otherwise turn a single sentence into many paragraphs. Rules:

    * a blank line separates paragraphs;
    * consecutive non-blank lines belong to the same paragraph and are
      joined with a space;
    * if a line ends with a hyphen, it is treated as a word split across
      lines and joined without a space (the hyphen is removed).
    """
    paragraphs = []
    current = ""

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            if current:
                paragraphs.append(current)
                current = ""
            continue

        if not current:
            current = line
        elif current.endswith("-"):
            # Word hyphenated across a line break: join without space.
            current = current[:-1] + line
        else:
            current = f"{current} {line}"

    if current:
        paragraphs.append(current)

    return paragraphs


def _render_text(text: str) -> str:
    return "\n".join(
        f"    <p>{html.escape(para)}</p>"
        for para in _reflow_paragraphs(text)
    )


def _render_image(href: str) -> str:
    return (
        '    <div class="illustration">\n'
        f'      <img src="{html.escape(href)}" alt=""/>\n'
        "    </div>"
    )


def _chapter_xhtml_blocks(title, blocks, image_refs, image_position) -> str:
    """Render a chapter's content blocks to XHTML.

    ``image_refs`` maps each ImageBlock (by identity) to its href. When
    ``image_position == "end"`` all images are appended after the text; when
    ``"inline"`` they are emitted in their original position in ``blocks``.
    """
    from .extract import ImageBlock, TextBlock

    href_by_id = {id(block): href for block, href in image_refs}
    body_parts = [f"    <h1>{html.escape(title)}</h1>"]

    has_any_text = any(
        isinstance(b, TextBlock) and b.text.strip() for b in blocks
    )
    if not has_any_text and not image_refs:
        body_parts.append("    <p>(sem conte\u00fado extra\u00eddo deste cap\u00edtulo)</p>")

    if image_position == "end":
        for block in blocks:
            if isinstance(block, TextBlock) and block.text.strip():
                body_parts.append(_render_text(block.text))
        for _block, href in image_refs:
            body_parts.append(_render_image(href))
    else:  # inline
        for block in blocks:
            if isinstance(block, TextBlock):
                if block.text.strip():
                    body_parts.append(_render_text(block.text))
            elif isinstance(block, ImageBlock):
                href = href_by_id.get(id(block))
                if href:
                    body_parts.append(_render_image(href))

    body = "\n".join(body_parts)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="pt-BR">\n'
        f"  <head>\n"
        f"    <title>{html.escape(title)}</title>\n"
        "    <style>\n"
        "      .illustration { text-align: center; margin: 1em 0; }\n"
        "      .illustration img { max-width: 100%; }\n"
        "    </style>\n"
        "  </head>\n"
        "  <body>\n"
        f"{body}\n"
        "  </body>\n"
        "</html>\n"
    )


def _nav_xhtml(meta: BookMeta, nav_points: list[str]) -> str:
    items = "\n".join(f"      {p}" for p in nav_points)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="pt-BR">\n'
        f"  <head><title>{html.escape(meta.title)}</title></head>\n"
        "  <body>\n"
        '    <nav epub:type="toc" id="toc">\n'
        f"      <h1>{html.escape(meta.title)}</h1>\n"
        "      <ol>\n"
        f"{items}\n"
        "      </ol>\n"
        "    </nav>\n"
        "  </body>\n"
        "</html>\n"
    )


def _content_opf(
    meta: BookMeta,
    manifest_items: list[str],
    spine_items: list[str],
    cover_meta: str = "",
) -> str:
    manifest = "\n    ".join(manifest_items)
    spine = "\n    ".join(spine_items)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"    <dc:identifier id=\"bookid\">{html.escape(meta.identifier)}</dc:identifier>\n"
        f"    <dc:title>{html.escape(meta.title)}</dc:title>\n"
        f"    <dc:creator>{html.escape(meta.author)}</dc:creator>\n"
        f"    <dc:language>{html.escape(meta.language)}</dc:language>{cover_meta}\n"
        '  </metadata>\n'
        "  <manifest>\n"
        f"    {manifest}\n"
        "  </manifest>\n"
        '  <spine>\n'
        f"    {spine}\n"
        "  </spine>\n"
        "</package>\n"
    )
