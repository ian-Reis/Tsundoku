from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse, unquote, urlencode

from bs4 import BeautifulSoup
from .base import BaseSiteHandler, SiteComicContext, SearchHit


class KappabeastSiteHandler(BaseSiteHandler):
    name = "kappabeast"
    display_name = "KappaBeast"
    domains = ("kappabeast.com", "www.kappabeast.com")

    BASE_URL = "https://kappabeast.com"
    API_URL = "https://strapi.kappabeast.com/api"
    _KEY_PATTERNS = (
        re.compile(r"""["']X-API-Key["']\s*:\s*["']([A-Za-z0-9._-]{16,256})["']"""),
        re.compile(r"""["']x-api-key["']\s*:\s*["']([A-Za-z0-9._-]{16,256})["']"""),
        re.compile(r"""(?:NEXT_PUBLIC_STRAPI_API_KEY|STRAPI_API_KEY|strapiApiKey|apiKey|api_key)\s*[:=]\s*["']([A-Za-z0-9._-]{16,256})["']"""),
    )

    def __init__(self) -> None:
        self._api_key: Optional[str] = None

    def _get_headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update(self._get_headers())

    def _store_api_key(self, scraper, key: str) -> str:
        key = (key or "").strip()
        if not key:
            raise RuntimeError("KappaBeast API key discovery returned an empty key.")
        self._api_key = key
        scraper.headers["X-API-Key"] = key
        return key

    def _extract_api_key(self, text: str) -> Optional[str]:
        if not text:
            return None
        for pattern in self._KEY_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1).strip()
        return None

    def _asset_urls_from_html(self, html: str) -> Iterable[str]:
        if not html:
            return ()

        soup = BeautifulSoup(html, "html.parser")
        urls: List[str] = []

        for tag in soup.find_all("script", src=True):
            urls.append(urljoin(self.BASE_URL, tag["src"]))

        for tag in soup.find_all("link", href=True):
            href = str(tag["href"])
            rel = " ".join(str(item).lower() for item in (tag.get("rel") or ()))
            as_attr = str(tag.get("as") or "").lower()
            if href.endswith(".js") or "modulepreload" in rel or as_attr == "script":
                urls.append(urljoin(self.BASE_URL, href))

        for match in re.finditer(r"""(?:src|href)=["']([^"']+\.js(?:\?[^"']*)?)["']""", html):
            urls.append(urljoin(self.BASE_URL, match.group(1)))

        seen = set()
        for url in urls:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                continue
            if parsed.netloc not in {"kappabeast.com", "www.kappabeast.com"}:
                continue
            if url in seen:
                continue
            seen.add(url)
            yield url

    def _read_frontend_text(self, url: str, scraper, make_request) -> str:
        response_text = ""
        first_error: Optional[Exception] = None

        try:
            response = make_request(url, scraper)
            response_text = getattr(response, "text", "") or ""
            status_code = getattr(response, "status_code", 200)
            if status_code < 400:
                return response_text
        except Exception as exc:
            first_error = exc

        try:
            from .crawlee_utils import fetch_html_with_cf_cookies, sync_cf_cookies

            text = fetch_html_with_cf_cookies(url, base_url=self.BASE_URL)
            sync_cf_cookies(scraper, self.BASE_URL)
            return text
        except Exception as exc:
            if response_text:
                return response_text
            if first_error:
                raise first_error
            raise exc

    def _discover_api_key(self, scraper, make_request) -> Optional[str]:
        html = self._read_frontend_text(self.BASE_URL, scraper, make_request)
        key = self._extract_api_key(html)
        if key:
            return key

        for asset_url in list(self._asset_urls_from_html(html))[:40]:
            asset_text = self._read_frontend_text(asset_url, scraper, make_request)
            key = self._extract_api_key(asset_text)
            if key:
                return key

        return None

    def _ensure_api_key(self, scraper, make_request) -> str:
        if self._api_key:
            scraper.headers["X-API-Key"] = self._api_key
            return self._api_key

        key = self._discover_api_key(scraper, make_request)
        if not key:
            raise RuntimeError(
                "KappaBeast API rejected the unauthenticated request, and no "
                "frontend X-API-Key could be discovered."
            )
        return self._store_api_key(scraper, key)

    def _is_auth_failure(self, response) -> bool:
        return getattr(response, "status_code", None) in {401, 403}

    def _api_request(self, url: str, scraper, make_request):
        if self._api_key:
            scraper.headers["X-API-Key"] = self._api_key

        try:
            response = make_request(url, scraper)
        except Exception as exc:
            response = getattr(exc, "response", None)
            if not self._is_auth_failure(response):
                raise
            scraper.headers.pop("X-API-Key", None)
            self._api_key = None
            self._ensure_api_key(scraper, make_request)
            return make_request(url, scraper)

        if not self._is_auth_failure(response):
            return response

        scraper.headers.pop("X-API-Key", None)
        self._api_key = None
        self._ensure_api_key(scraper, make_request)
        retry_response = make_request(url, scraper)
        if self._is_auth_failure(retry_response):
            raise_for_status = getattr(retry_response, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
            raise RuntimeError("KappaBeast API rejected the request after dynamic key discovery.")
        return retry_response

    def _slug_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        path = unquote(parsed.path).strip("/")
        parts = path.split("/")
        if not parts:
            raise ValueError(f"Could not extract slug from URL: {url}")
        
        # Handle formats like /series/manga-slug or /manga/manga-slug or just /manga-slug
        if len(parts) >= 2 and parts[0] in ("series", "manga", "comic"):
            return parts[1]
        return parts[-1]

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        slug = self._slug_from_url(url)
        api_url = f"{self.API_URL}/mangas?filters[slug][$eq]={slug}&populate[media][populate]=*"
        
        response = self._api_request(api_url, scraper, make_request)
        data = response.json()
        
        items = data.get("data") or []
        if not items:
            raise RuntimeError(f"KappaBeast manga not found with slug: {slug}")
            
        item = items[0]
        title = item.get("title") or slug
        
        cover = None
        media = item.get("media")
        if media and isinstance(media, list) and len(media) > 0:
            cover_img = media[0].get("coverImage")
            if cover_img and cover_img.get("url"):
                cover = "https://strapi.kappabeast.com" + cover_img["url"]

        comic = {
            "url": url,
            "title": title,
            "cover": cover,
            "documentId": item.get("documentId"),
            "id": item.get("id"),
            "slug": slug,
        }
        
        return SiteComicContext(
            comic=comic,
            title=title,
            identifier=slug,
            soup=None,
        )

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        doc_id = context.comic.get("documentId")
        if not doc_id:
            raise ValueError("Manga documentId not found in context.")
            
        chapters = []
        page = 1
        while True:
            api_url = f"{self.API_URL}/chapters"
            params = {
                "filters[manga][documentId][$eq]": doc_id,
                "sort[0]": "number:asc",
                "pagination[page]": page,
                "pagination[pageSize]": 100,
                "populate": "manga"
            }
            query_str = urlencode(params, doseq=True)
            full_url = f"{api_url}?{query_str}"
            
            response = self._api_request(full_url, scraper, make_request)
            data = response.json()
            
            items = data.get("data") or []
            if not items:
                break
                
            for item in items:
                num = item.get("number")
                title = item.get("title")
                
                # Check for early access or paid restrictions (if any)
                # Usually Kappabeast chapters are all free or unlocked via standard API.
                chapters.append({
                    "id": item.get("id"),
                    "documentId": item.get("documentId"),
                    "chap": str(num),
                    "title": title or "",
                    "url": f"https://kappabeast.com/series/{context.identifier}/chapter/{num}",
                    "htmlContent": item.get("htmlContent") or "",
                })
                
            pagination = data.get("meta", {}).get("pagination") or {}
            page_count = pagination.get("pageCount") or 1
            if page >= page_count:
                break
            page += 1
            
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        html = chapter.get("htmlContent") or ""
        if not html:
            chap_id = chapter.get("id")
            if chap_id:
                api_url = f"{self.API_URL}/chapters/{chap_id}?populate=*"
                response = self._api_request(api_url, scraper, make_request)
                data = response.json()
                html = data.get("data", {}).get("htmlContent") or ""
                
        if not html:
            return []
            
        soup = BeautifulSoup(html, "html.parser")
        images = []
        
        for tag in soup.find_all(["a", "img"]):
            if tag.name == "a":
                href = tag.get("href", "")
                if href and ("googleusercontent" in href or any(href.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp"))):
                    if href not in images:
                        images.append(href)
            elif tag.name == "img":
                src = tag.get("src", "")
                if src:
                    parent_a = tag.find_parent("a")
                    if parent_a:
                        parent_href = parent_a.get("href", "")
                        if parent_href in images:
                            continue
                    if src not in images:
                        images.append(src)
                        
        return images

    def search(
        self,
        query: str,
        scraper,
        make_request,
        *,
        language: str = "en",
        limit: int = 20,
    ) -> List[SearchHit]:
        from urllib.parse import quote
        clean_query = (query or "").strip()
        if not clean_query:
            return []
            
        encoded_query = quote(clean_query)
        api_url = f"{self.API_URL}/mangas?filters[title][$containsi]={encoded_query}&populate[media][populate]=*&pagination[pageSize]={limit}"
        
        try:
            response = self._api_request(api_url, scraper, make_request)
            data = response.json()
        except Exception:
            return []
            
        items = data.get("data") or []
        hits = []
        for idx, item in enumerate(items):
            title = item.get("title") or ""
            slug = item.get("slug") or ""
            if not title or not slug:
                continue
                
            cover = None
            media = item.get("media")
            if media and isinstance(media, list) and len(media) > 0:
                cover_img = media[0].get("coverImage")
                if cover_img and cover_img.get("url"):
                    cover = "https://strapi.kappabeast.com" + cover_img["url"]
                    
            alt_titles = []
            alt_title_str = item.get("altTitle")
            if alt_title_str:
                alt_titles = [t.strip() for t in alt_title_str.split(",") if t.strip()]
                
            url = f"https://kappabeast.com/series/{slug}"
            raw_score = max(0.05, 1.0 - (idx / max(1, len(items))))
            
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=url,
                    cover=cover,
                    alt_titles=alt_titles,
                    year=item.get("releaseYear"),
                    language="en",
                    raw_score=raw_score,
                )
            )
        return hits
