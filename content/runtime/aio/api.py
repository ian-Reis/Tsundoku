from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from typing import Optional
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import sites
from aio_config import resolve_output_dir
from library_state import scan_library, to_jsonable
from metadata_editor import read_metadata, update_metadata
from sites import get_handler_by_name, get_handler_for_url

app = FastAPI(title="AIO Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("AIO_CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR = os.path.join(tempfile.gettempdir(), "aio_webtoon_api")
os.makedirs(TEMP_DIR, exist_ok=True)


def cleanup_temp_files() -> None:
    while True:
        now = time.time()
        for fname in os.listdir(TEMP_DIR):
            fpath = os.path.join(TEMP_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                if now - os.path.getmtime(fpath) > 3600:
                    os.remove(fpath)
            except Exception:
                pass
        time.sleep(600)


threading.Thread(target=cleanup_temp_files, daemon=True).start()


def get_scraper():
    try:
        import cloudscraper

        return cloudscraper.create_scraper()
    except Exception:
        return requests.Session()


def make_request(url: str, scraper):
    return scraper.get(url, timeout=30)


def _handler_for(url: str, site: Optional[str] = None):
    handler = get_handler_by_name(site) if site else get_handler_for_url(url)
    if not handler:
        raise HTTPException(404, "No handler found for this site")
    return handler


def _allowed_image_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return False
    if host.startswith("10.") or host.startswith("192.168.") or host.startswith("169.254."):
        return False
    for handler in getattr(sites, "_REGISTERED_HANDLERS", []):
        for domain in getattr(handler, "domains", ()) or ():
            if host == domain or host.endswith("." + domain):
                return True
    return True


@app.get("/api/handlers")
def list_handlers():
    handlers = []
    for handler in getattr(sites, "_REGISTERED_HANDLERS", []):
        handlers.append(
            {
                "name": getattr(handler, "name", None),
                "display_name": getattr(handler, "display_name", None),
                "base_url": getattr(handler, "base_url", None),
                "domains": list(getattr(handler, "domains", ()) or ()),
                "search_capable": type(handler).search is not sites.BaseSiteHandler.search,
            }
        )
    return handlers


@app.get("/api/info")
def get_comic_info(url: str, site: Optional[str] = None):
    handler = _handler_for(url, site)
    scraper = get_scraper()
    handler.configure_session(scraper, None)
    context = handler.fetch_comic_context(url, scraper, make_request)
    return context.comic


@app.get("/api/chapters")
def get_chapters(
    url: str,
    site: Optional[str] = None,
    language: str = "en",
    type: str = "chapter",
):
    handler = _handler_for(url, site)
    scraper = get_scraper()
    handler.configure_session(scraper, None)
    context = handler.fetch_comic_context(url, scraper, make_request)
    if type == "volume":
        volumes = handler.get_volumes(context, scraper, language, make_request)
        if not volumes:
            raise HTTPException(501, "This handler does not expose volume listing")
        return volumes
    return handler.get_chapters(context, scraper, language, make_request)


@app.get("/api/chapter_images")
def get_chapter_images(url: str, chapter_id: str, site: Optional[str] = None):
    handler = _handler_for(url, site)
    scraper = get_scraper()
    handler.configure_session(scraper, None)
    context = handler.fetch_comic_context(url, scraper, make_request)
    chapters = handler.get_chapters(context, scraper, "en", make_request)
    chapter = next(
        (
            c
            for c in chapters
            if str(c.get("id") or c.get("hid") or c.get("url") or c.get("chap")) == str(chapter_id)
        ),
        None,
    )
    if not chapter:
        raise HTTPException(404, "Chapter not found")
    return {"images": handler.get_chapter_images(chapter, scraper, make_request)}


@app.get("/api/search")
def search_comics(
    query: str,
    language: str = "en",
    limit: int = 20,
    parallelism: int = 6,
):
    from sites.search_orchestrator import ImageQualityCache, ProbeFailureCache, search_all

    def scraper_factory(handler):
        scraper = get_scraper()
        handler.configure_session(scraper, None)
        return scraper

    candidates = search_all(
        query,
        scraper_factory,
        make_request,
        language=language,
        parallelism=parallelism,
        top_per_site=limit,
        min_match=0.55,
        probe_failure_cache=ProbeFailureCache(),
        img_quality_cache=ImageQualityCache(),
    )
    return {"query": query, "candidates": [candidate.to_json() for candidate in candidates]}


@app.get("/api/library")
def get_library(output_dir: Optional[str] = None):
    return to_jsonable(scan_library(resolve_output_dir(output_dir)))


@app.get("/api/metadata")
def get_metadata(path: str):
    if not os.path.isfile(path):
        raise HTTPException(404, "File not found")
    return read_metadata(path)


@app.post("/api/metadata")
def post_metadata(payload: dict):
    path = payload.get("path")
    data = payload.get("data") or {}
    cover_path = payload.get("cover_path")
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "File not found")
    update_metadata(path, data, cover_path)
    return {"ok": True}


@app.get("/api/download_image")
def download_image(url: str):
    if not _allowed_image_url(url):
        raise HTTPException(400, "Image URL is not allowed")
    ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
    fname = f"img_{uuid.uuid4().hex}{ext}"
    fpath = os.path.join(TEMP_DIR, fname)
    scraper = get_scraper()
    try:
        response = scraper.get(url, stream=True, timeout=30)
        response.raise_for_status()
        with open(fpath, "wb") as handle:
            for chunk in response.iter_content(131072):
                if chunk:
                    handle.write(chunk)
    except Exception as exc:
        raise HTTPException(500, f"Failed to download image: {exc}") from exc
    return {"url": f"/api/temp/{fname}"}


@app.get("/api/temp/{filename}")
def serve_temp_file(filename: str):
    safe_name = os.path.basename(filename)
    fpath = os.path.abspath(os.path.join(TEMP_DIR, safe_name))
    temp_root = os.path.abspath(TEMP_DIR)
    if not fpath.startswith(temp_root + os.sep) or not os.path.isfile(fpath):
        raise HTTPException(404, "File not found")
    return FileResponse(fpath)
