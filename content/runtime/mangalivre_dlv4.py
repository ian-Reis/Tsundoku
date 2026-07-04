#!/usr/bin/env python3
"""
mangalivre_dl.py — v4
Downloader robusto para mangalivre.to e outros sites baseados no tema Madara.

Novidades da v4:
  - Suporte a --status-file: escreve progresso em JSON atômico pra um front-end
    externo (o app Godot "Tsundoku") acompanhar via polling. Sem --status-file,
    comporta-se igual à v3.
  - Correção de encoding do console no Windows (cp1252 → UTF-8).

Novidades da v3:
  - Stealth de verdade via curl_cffi (TLS fingerprint de Chrome real)
  - Cadeia de fallback: curl_cffi → cloudscraper → requests (não quebra nada)
  - Token bucket rate limiter (taxa sustentada controlada com burst inicial)
  - Adaptive backoff + circuit breaker (lentifica/pausa diante de falhas)
  - Pausas comportamentais (intervalos longos a cada N caps)
  - Download paralelo de imagens dentro do capítulo (4 conexões, como browser)
  - Progress bar com tqdm + estatísticas finais
  - Logging configurável (--verbose, --log arquivo.log)
  - Modo --probe pra validar antes de baixar centenas

Requisitos:
  pip install requests beautifulsoup4
Opcionais (altamente recomendados):
  pip install curl_cffi tqdm
Fallback se não tiver curl_cffi:
  pip install cloudscraper

Uso:
  python mangalivre_dl.py "https://mangalivre.to/manga/<slug>/" --probe
  python mangalivre_dl.py "https://mangalivre.to/manga/<slug>/" --chapters 54-218
  python mangalivre_dl.py "https://mangalivre.to/manga/<slug>/" --log run.log --verbose

Com acompanhamento de progresso (pro front-end):
  python mangalivre_dl.py "<url>" --chapters 1-40 --output downloads \
      --status-file temp/status.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

# ============================================================================ #
# Encoding do console (Windows)
# ============================================================================ #
# O console do Windows usa cp1252 (charmap) por padrão, que quebra ao imprimir
# caracteres especiais — títulos, emojis do log (★ ⏸ ✓), URLs com acento, etc.
# O UnicodeEncodeError resultante, por acontecer dentro do bloco de request,
# era confundido com falha de rede e disparava retries inúteis. Forçar UTF-8
# no stdout/stderr resolve na raiz. errors="replace" é seguro extra: um
# caractere impossível vira '?' em vez de derrubar o script. reconfigure()
# existe desde o Python 3.7.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

# ============================================================================ #
# Dependências
# ============================================================================ #
try:
    from bs4 import BeautifulSoup
    import requests
except ImportError as e:
    sys.exit(f"Faltando dependência: {e.name}. Instale: pip install requests beautifulsoup4")

# Backend HTTP (em ordem de preferência)
_curl_cffi = None
_cloudscraper = None
try:
    from curl_cffi import requests as _curl_cffi  # type: ignore
except ImportError:
    pass
try:
    import cloudscraper as _cloudscraper  # type: ignore
except ImportError:
    pass

try:
    from tqdm import tqdm  # type: ignore
    HAS_TQDM = True
    try:
        from tqdm.contrib.logging import logging_redirect_tqdm  # type: ignore
    except ImportError:
        from contextlib import contextmanager

        @contextmanager
        def logging_redirect_tqdm():  # type: ignore[no-redef]
            yield
except ImportError:
    HAS_TQDM = False
    from contextlib import contextmanager

    @contextmanager
    def logging_redirect_tqdm():
        yield


# ============================================================================ #
# Constantes
# ============================================================================ #
# curl_cffi: target de impersonação. chrome124 é estável e amplamente suportado.
IMPERSONATE_TARGET = "chrome124"

# Status HTTP que disparam retry (transientes / Cloudflare / rate limit)
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

# Códigos que indicam Cloudflare challenge ativo (não vale retry imediato)
CLOUDFLARE_BLOCK = {403, 503}  # 503 com challenge

# Headers de Firefox 132 realista (usados quando NÃO há curl_cffi)
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Priority": "u=0, i",
}

# Headers ajustados para requests de imagem
IMAGE_HEADER_OVERRIDES = {
    "Accept": "image/avif,image/webp,image/png,image/svg+xml,image/*;q=0.8,*/*;q=0.5",
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "same-origin",
}

# Patterns pra extrair número de capítulo
CHAPTER_NUM_PATTERNS = (
    re.compile(r"cap[íi]tulo[\s\-_]*(\d+(?:[.,]\d+)?)", re.I),
    re.compile(r"chapter[\s\-_]*(\d+(?:[.,]\d+)?)", re.I),
    re.compile(r"cap[\s\-_]*(\d+(?:[.,]\d+)?)", re.I),
)


# ============================================================================ #
# Logger
# ============================================================================ #
log = logging.getLogger("mangalivre_dl")


def setup_logging(verbose: bool, log_file: Optional[str]) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    # Blindagem extra contra encoding: se um caractere escapar do reconfigure
    # UTF-8 acima, troca em vez de lançar UnicodeEncodeError (que antes era
    # erroneamente tratado como falha de rede pelo retry wrapper).
    console.encoding = "utf-8"
    console.errors = "replace"
    log.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)  # arquivo sempre tem o máximo de detalhe
        fh.setFormatter(
            logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s")
        )
        log.addHandler(fh)


# ============================================================================ #
# Status reporter (arquivo JSON pra consumo externo, ex: front-end Godot)
# ============================================================================ #
class StatusReporter:
    """Escreve o progresso num arquivo JSON de forma atômica, pra que um
    processo externo (o app Godot) faça polling sem risco de ler um JSON
    parcialmente escrito. Se status_file for None, vira no-op — o script
    roda normalmente pela linha de comando sem --status-file.

    A escrita é atômica (escreve em .tmp e usa os.replace) porque o front-end
    pode ler o arquivo no exato instante da escrita. os.replace é atômico tanto
    no Windows quanto no Linux, então o leitor sempre vê o JSON antigo completo
    ou o novo completo — nunca um estado parcial.
    """

    def __init__(self, status_file: Optional[str]):
        self.path = Path(status_file) if status_file else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, status: str, **fields) -> None:
        if self.path is None:
            return

        payload = {"status": status, "timestamp": time.time(), **fields}

        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, str(self.path))
        except Exception as e:
            # Nunca deixa o reporter derrubar o download — só loga e segue.
            log.debug("StatusReporter falhou ao escrever: %s", e)
            if tmp is not None and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass


# ============================================================================ #
# Token bucket rate limiter
# ============================================================================ #
class RateLimiter:
    """
    Limita a taxa média de requests com tolerância a bursts.
    Ex: rate=1.0, burst=3 → 3 requests instantâneos, depois 1/s sustentado.
    """

    def __init__(self, rate: float = 1.0, burst: int = 3):
        self.rate = rate
        self.burst = float(burst)
        self.tokens = float(burst)
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> float:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_refill = now

            if self.tokens >= 1:
                self.tokens -= 1
                return 0.0

            wait = (1.0 - self.tokens) / self.rate
            self.tokens = 0.0
            self.last_refill = now + wait

        time.sleep(wait)
        return wait


# ============================================================================ #
# Adaptive backoff + circuit breaker
# ============================================================================ #
class AdaptiveGuard:
    """
    Acompanha falhas consecutivas. Aumenta o delay extra progressivamente e
    dispara circuit breaker se ficar muito ruim (vários caps seguidos quebrando).
    """

    def __init__(self, fail_threshold: int = 5, breaker_cooldown: float = 300.0):
        self.consecutive = 0
        self.total_fails = 0
        self.total_success = 0
        self.fail_threshold = fail_threshold
        self.breaker_cooldown = breaker_cooldown
        self.tripped_at: Optional[float] = None
        self.lock = threading.Lock()

    def record_success(self) -> None:
        with self.lock:
            self.consecutive = 0
            self.total_success += 1

    def record_failure(self) -> int:
        with self.lock:
            self.consecutive += 1
            self.total_fails += 1
            return self.consecutive

    def extra_delay(self) -> float:
        with self.lock:
            if self.consecutive == 0:
                return 0.0
            # +3s por falha consecutiva, capado em 60s
            return min(60.0, self.consecutive * 3.0)

    def should_trip(self) -> bool:
        with self.lock:
            return self.consecutive >= self.fail_threshold

    def trip(self) -> None:
        with self.lock:
            self.tripped_at = time.monotonic()
        log.warning(
            "Circuit breaker disparado (%d falhas seguidas). Pausando %ds...",
            self.consecutive,
            int(self.breaker_cooldown),
        )
        time.sleep(self.breaker_cooldown)
        with self.lock:
            self.consecutive = 0  # reseta após cooldown


# ============================================================================ #
# Stats
# ============================================================================ #
@dataclass
class Stats:
    chapters_done: int = 0
    chapters_failed: int = 0
    chapters_skipped: int = 0
    images_ok: int = 0
    images_fail: int = 0
    bytes_total: int = 0
    retries: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def speed_mb(self) -> float:
        el = self.elapsed()
        return (self.bytes_total / 1024 / 1024) / el if el > 0 else 0.0

    def render(self) -> str:
        el = self.elapsed()
        h, rem = divmod(int(el), 3600)
        m, s = divmod(rem, 60)
        return (
            f"\n{'─' * 60}\n"
            f"  Tempo:           {h:02d}:{m:02d}:{s:02d}\n"
            f"  Capítulos OK:    {self.chapters_done}\n"
            f"  Capítulos skip:  {self.chapters_skipped}\n"
            f"  Capítulos fail:  {self.chapters_failed}\n"
            f"  Imagens OK:      {self.images_ok}\n"
            f"  Imagens fail:    {self.images_fail}\n"
            f"  Total baixado:   {self.bytes_total / 1024 / 1024:.1f} MB\n"
            f"  Velocidade:      {self.speed_mb():.2f} MB/s\n"
            f"  Retries:         {self.retries}\n"
            f"{'─' * 60}"
        )


# ============================================================================ #
# Session factory
# ============================================================================ #
def make_session():
    """Cria a melhor sessão HTTP disponível, fazendo fallback automático."""
    if _curl_cffi is not None:
        log.info("HTTP backend: curl_cffi (impersonate=%s) ★", IMPERSONATE_TARGET)
        sess = _curl_cffi.Session(impersonate=IMPERSONATE_TARGET)
        sess.headers.update({"Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"})
        return sess

    if _cloudscraper is not None:
        log.info("HTTP backend: cloudscraper (sem TLS fingerprint completo)")
        sess = _cloudscraper.create_scraper(
            browser={"browser": "firefox", "platform": "linux", "desktop": True}
        )
        return sess

    log.info("HTTP backend: requests (fallback básico — instale curl_cffi)")
    sess = requests.Session()
    sess.headers.update(BROWSER_HEADERS)
    return sess


# ============================================================================ #
# Retry wrapper
# ============================================================================ #
def http_request(
    session,
    method: str,
    url: str,
    *,
    rate_limiter: Optional[RateLimiter] = None,
    guard: Optional[AdaptiveGuard] = None,
    stats: Optional[Stats] = None,
    max_retries: int = 5,
    **kwargs,
):
    """
    Faz uma request com:
      - Rate limiting (token bucket)
      - Retry com backoff exponencial + jitter
      - Backoff adaptativo extra após falhas consecutivas
      - Respeita Retry-After do servidor
    """
    last_error: Optional[Exception] = None

    for attempt in range(max_retries):
        if rate_limiter is not None:
            waited = rate_limiter.acquire()
            if waited > 0.1:
                log.debug("Rate limiter: aguardei %.2fs", waited)

        if guard is not None:
            extra = guard.extra_delay()
            if extra > 0:
                log.debug("Adaptive backoff: +%.1fs", extra)
                time.sleep(extra)

        try:
            r = session.request(method, url, **kwargs)
            status = getattr(r, "status_code", None)

            if status in RETRY_STATUS:
                last_error = Exception(f"HTTP {status}")
                if stats:
                    stats.retries += 1

                ra = (r.headers.get("Retry-After") or "").strip()
                if ra and ra.isdigit():
                    wait = min(float(ra), 120.0)
                else:
                    wait = (2 ** attempt) + random.uniform(0, 1.5)

                log.warning(
                    "[%s] HTTP %s, retry %d/%d em %.1fs",
                    _short_url(url), status, attempt + 1, max_retries, wait,
                )
                time.sleep(wait)
                continue

            # Sucesso: deixa o caller chamar raise_for_status se quiser
            r.raise_for_status()
            if guard:
                guard.record_success()
            return r

        except KeyboardInterrupt:
            raise
        except Exception as e:
            last_error = e
            if stats:
                stats.retries += 1
            if attempt == max_retries - 1:
                break
            wait = (2 ** attempt) + random.uniform(0, 1.5)
            log.warning(
                "[%s] %s: %s, retry %d/%d em %.1fs",
                _short_url(url),
                type(e).__name__,
                str(e)[:80],
                attempt + 1,
                max_retries,
                wait,
            )
            time.sleep(wait)

    if guard:
        guard.record_failure()
    raise last_error if last_error else RuntimeError("falha sem causa identificável")


def _short_url(url: str) -> str:
    """Encurta URL pra log ficar legível."""
    try:
        p = urlparse(url)
        return p.path.rsplit("/", 1)[-1] or p.netloc
    except Exception:
        return url[-40:]


# ============================================================================ #
# Scraping (Madara)
# ============================================================================ #
def fetch_manga_page(session, manga_url: str, **kw) -> tuple[str, Optional[str], str]:
    """Retorna (título, post_id, html_da_página)."""
    r = http_request(session, "GET", manga_url, timeout=30, **kw)
    soup = BeautifulSoup(r.text, "html.parser")

    title = None
    for sel in ("div.post-title h1", "div.post-title h3", ".manga-title h1", "h1"):
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            break
    title = title or "manga"

    post_id = None
    for el in soup.find_all(attrs={"data-post-id": True}):
        post_id = el.get("data-post-id")
        if post_id:
            break
    if not post_id:
        inp = soup.find("input", {"class": re.compile(r"rating-post-id")})
        if inp and inp.get("value"):
            post_id = inp["value"]
    if not post_id:
        holder = soup.find(id="manga-chapters-holder")
        if holder and holder.get("data-id"):
            post_id = holder["data-id"]

    log.debug("Página do mangá: title=%r post_id=%r", title, post_id)
    return title, post_id, r.text


def fetch_chapter_list(
    session, manga_url: str, post_id: Optional[str], manga_html: str, **kw
):
    base = manga_url.rstrip("/") + "/"

    # Método 1: <manga_url>/ajax/chapters/  (Madara moderno)
    for path in ("ajax/chapters/", "ajax/chapters"):
        try:
            r = http_request(
                session, "POST", urljoin(base, path), max_retries=3, timeout=30, **kw
            )
            chapters = parse_chapter_html(r.text)
            if chapters:
                log.debug("Capítulos obtidos via %s (%d)", path, len(chapters))
                return chapters
        except Exception as e:
            log.debug("ajax/%s falhou: %s", path, e)

    # Método 2: /wp-admin/admin-ajax.php  (Madara antigo)
    if post_id:
        parsed = urlparse(manga_url)
        admin = f"{parsed.scheme}://{parsed.netloc}/wp-admin/admin-ajax.php"
        try:
            r = http_request(
                session, "POST", admin,
                data={"action": "manga_get_chapters", "manga": post_id},
                max_retries=3, timeout=30, **kw,
            )
            chapters = parse_chapter_html(r.text)
            if chapters:
                log.debug("Capítulos obtidos via admin-ajax (%d)", len(chapters))
                return chapters
        except Exception as e:
            log.debug("admin-ajax falhou: %s", e)

    # Método 3: HTML da própria página
    chapters = parse_chapter_html(manga_html)
    log.debug("Capítulos obtidos do HTML da página (%d)", len(chapters))
    return chapters


def parse_chapter_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    chapters = []
    for a in soup.select("li.wp-manga-chapter a, .wp-manga-chapter a"):
        href = (a.get("href") or "").strip()
        text = a.get_text(" ", strip=True)
        if not href or "javascript" in href.lower():
            continue
        chapters.append({
            "num": extract_chapter_number(text, href),
            "title": text,
            "url": href,
        })
    chapters.sort(key=lambda c: c["num"])
    return chapters


def extract_chapter_number(text: str, href: str) -> float:
    for pattern in CHAPTER_NUM_PATTERNS:
        m = pattern.search(text)
        if m:
            return float(m.group(1).replace(",", "."))
    m = re.search(r"(?:capitulo|chapter|cap)[\-_](\d+(?:[.,]\d+)?)", href, re.I)
    if m:
        return float(m.group(1).replace(",", "."))
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    return float(m.group(1).replace(",", ".")) if m else 0.0


def fetch_chapter_images(session, chapter_url: str, manga_url: str, **kw) -> list[str]:
    sep = "&" if "?" in chapter_url else "?"
    url = f"{chapter_url}{sep}style=list"

    headers = kw.pop("headers", {}) or {}
    headers.setdefault("Referer", manga_url)
    r = http_request(session, "GET", url, headers=headers, timeout=30, **kw)
    soup = BeautifulSoup(r.text, "html.parser")

    selectors = (
        ".reading-content img",
        ".page-break img",
        ".wp-manga-chapter-img",
        "div.text-left img",
    )
    seen: set[str] = set()
    images: list[str] = []
    for sel in selectors:
        for img in soup.select(sel):
            src = (
                img.get("data-src")
                or img.get("data-lazy-src")
                or (img.get("srcset", "").split()[0] if img.get("srcset") else None)
                or img.get("src")
            )
            if not src:
                continue
            src = src.strip()
            if not src or src.startswith("data:") or src in seen:
                continue
            seen.add(src)
            images.append(src)
        if images:
            break
    return images


# ============================================================================ #
# Filtros / utils
# ============================================================================ #
def filter_chapters(filter_str: str, all_chapters: list[dict]) -> list[dict]:
    if not filter_str or filter_str.lower() == "all":
        return all_chapters

    selected: list[dict] = []
    for part in filter_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a_s, b_s = part.split("-", 1)
            a = float(a_s) if a_s else float("-inf")
            b = float(b_s) if b_s else float("inf")
            selected.extend(c for c in all_chapters if a <= c["num"] <= b)
        else:
            n = float(part)
            selected.extend(c for c in all_chapters if c["num"] == n)

    seen_urls, result = set(), []
    for c in selected:
        if c["url"] not in seen_urls:
            seen_urls.add(c["url"])
            result.append(c)
    return result


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip().rstrip(".")[:180]


def format_chapter_label(num: float) -> str:
    if num == int(num):
        return f"{int(num):04d}"
    return f"{num:07.2f}"


# ============================================================================ #
# Download de imagens (paralelo dentro do capítulo)
# ============================================================================ #
def download_chapter_images(
    session,
    images: list[str],
    chapter_url: str,
    out_dir: Path,
    *,
    rate_limiter: RateLimiter,
    guard: AdaptiveGuard,
    stats: Stats,
    workers: int = 4,
    progress_label: str = "",
) -> tuple[int, int]:
    """Baixa todas as imagens em paralelo (até `workers` conexões). Retorna (ok, fail)."""
    ok_count = 0
    fail_count = 0
    bytes_lock = threading.Lock()

    # Barra interna: aparece embaixo da barra de capítulos, some quando termina
    img_bar = None
    if HAS_TQDM:
        img_bar = tqdm(
            total=len(images),
            desc=progress_label or "Imagens",
            unit="img",
            leave=False,
            position=1,
            ncols=100,
            bar_format="{desc:<14} {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} imgs",
        )

    def _dl_one(idx: int, img_url: str):
        nonlocal ok_count, fail_count
        ext = Path(urlparse(img_url).path).suffix.lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"):
            ext = ".jpg"
        dest = out_dir / f"{idx:03d}{ext}"

        if dest.exists() and dest.stat().st_size > 0:
            return True, 0

        headers = dict(IMAGE_HEADER_OVERRIDES)
        headers["Referer"] = chapter_url

        try:
            r = http_request(
                session, "GET", img_url,
                headers=headers, stream=True, timeout=60,
                rate_limiter=rate_limiter, guard=guard, stats=stats,
            )
            written = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
            with bytes_lock:
                stats.bytes_total += written
            return True, written
        except Exception as e:
            log.warning("imagem %d falhou: %s", idx, str(e)[:80])
            try:
                if dest.exists():
                    dest.unlink()
            except Exception:
                pass
            return False, 0
        finally:
            if img_bar is not None:
                img_bar.update(1)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_dl_one, i + 1, u) for i, u in enumerate(images)]
            for fut in futures:
                ok, _ = fut.result()
                if ok:
                    ok_count += 1
                else:
                    fail_count += 1
    finally:
        if img_bar is not None:
            img_bar.close()

    stats.images_ok += ok_count
    stats.images_fail += fail_count
    return ok_count, fail_count


# ============================================================================ #
# CBZ
# ============================================================================ #
def make_cbz(image_dir: Path, cbz_path: Path) -> None:
    images = sorted(p for p in image_dir.iterdir() if p.is_file())
    with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, img in enumerate(images, 1):
            zf.write(img, f"{i:03d}{img.suffix.lower()}")


# ============================================================================ #
# Pausas comportamentais
# ============================================================================ #
def behavioral_pause_if_needed(idx: int, total: int) -> None:
    """
    A cada ~25 capítulos, faz uma pausa mais longa (30-90s). Simula
    comportamento humano (intervalo, troca de aba) e dá respiro ao servidor.
    """
    if idx == 0 or idx >= total:
        return
    # ~25 caps em média, mas randomizado (não cair em múltiplos exatos)
    if idx % 25 == 0 and random.random() < 0.85:
        pause = random.uniform(30.0, 90.0)
        log.info("⏸  Pausa comportamental: %.0fs (cap %d/%d)", pause, idx, total)
        time.sleep(pause)


# ============================================================================ #
# Main
# ============================================================================ #
def main() -> int:
    p = argparse.ArgumentParser(
        description="Downloader v4 para mangalivre.to e outros sites Madara",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", help="URL do mangá (ex: https://mangalivre.to/manga/<slug>/)")
    p.add_argument("--chapters", default="all", help="Filtro: 'all', '1-10', '1,3,5', '5-', '-10'")
    p.add_argument("--output", default="./downloads", help="Pasta de saída (padrão: ./downloads)")
    p.add_argument("--format", choices=["cbz", "folder"], default="cbz")
    p.add_argument("--status-file", default=None,
                   help="Escreve progresso em JSON neste caminho (pra front-ends "
                        "tipo o app Godot fazerem polling). Escrita atômica, opcional.")
    p.add_argument("--list", action="store_true", help="Só lista capítulos, não baixa")
    p.add_argument("--probe", action="store_true",
                   help="Modo teste: baixa só o primeiro capítulo do filtro pra validar setup")
    p.add_argument("--rate", type=float, default=1.0,
                   help="Requests/segundo sustentado (padrão: 1.0)")
    p.add_argument("--burst", type=int, default=3,
                   help="Tokens iniciais do bucket (padrão: 3)")
    p.add_argument("--image-workers", type=int, default=4,
                   help="Imagens em paralelo dentro do capítulo (padrão: 4, como browser)")
    p.add_argument("--max-retries", type=int, default=5,
                   help="Tentativas máximas por request (padrão: 5)")
    p.add_argument("--no-behavioral-pause", action="store_true",
                   help="Desativa pausas longas a cada ~25 caps")
    p.add_argument("--verbose", "-v", action="store_true", help="Loga retries e detalhes")
    p.add_argument("--log", metavar="FILE", help="Salva log completo em arquivo")
    args = p.parse_args()

    setup_logging(args.verbose, args.log)

    reporter = StatusReporter(args.status_file)

    if not HAS_TQDM:
        log.info("dica: pip install tqdm  → barra de progresso melhor")
    if _curl_cffi is None:
        log.warning(
            "curl_cffi NÃO instalado — recomendado pra stealth: pip install curl_cffi"
        )

    rate_limiter = RateLimiter(rate=args.rate, burst=args.burst)
    guard = AdaptiveGuard(fail_threshold=5, breaker_cooldown=300.0)
    stats = Stats()
    session = make_session()

    log.info("→ %s", args.url)
    reporter.write("starting", url=args.url)
    try:
        title, post_id, manga_html = fetch_manga_page(
            session, args.url,
            rate_limiter=rate_limiter, guard=guard, stats=stats,
            max_retries=args.max_retries,
        )
    except Exception as e:
        log.error("falha carregando página do mangá: %s", e)
        reporter.write("error", message=f"falha carregando página do mangá: {e}")
        return 1
    log.info("Título: %s", title)

    try:
        chapters = fetch_chapter_list(
            session, args.url, post_id, manga_html,
            rate_limiter=rate_limiter, guard=guard, stats=stats,
        )
    except Exception as e:
        log.error("falha listando capítulos: %s", e)
        reporter.write("error", message=f"falha listando capítulos: {e}")
        return 1

    if not chapters:
        log.error("Nenhum capítulo encontrado. Se o site usa Cloudflare desafiador, "
                  "instale curl_cffi (pip install curl_cffi).")
        reporter.write("error", message="nenhum capítulo encontrado")
        return 1

    log.info("%d capítulos no catálogo (cap %s → %s)",
             len(chapters), chapters[0]["num"], chapters[-1]["num"])

    selected = filter_chapters(args.chapters, chapters)
    if not selected:
        log.error("Nenhum capítulo dentro do filtro: %s", args.chapters)
        reporter.write("error", message="nenhum capítulo dentro do filtro")
        return 1

    if args.probe:
        selected = selected[:1]
        log.info("MODO PROBE: baixando só o primeiro do filtro pra validar setup")

    log.info("%d capítulos selecionados", len(selected))

    if args.list:
        for c in selected:
            print(f"  - Cap {c['num']:>6}: {c['title']}")
        return 0

    reporter.write(
        "preparing",
        title=title,
        total_chapters=len(selected),
        progress=0,
    )

    safe_title = sanitize_filename(title)
    manga_dir = Path(args.output) / safe_title
    manga_dir.mkdir(parents=True, exist_ok=True)

    failed: list[dict] = []
    total_sel = len(selected)

    # Iterador com tqdm se disponível
    if HAS_TQDM:
        iterator = tqdm(
            enumerate(selected, 1),
            total=total_sel,
            unit="cap",
            desc=safe_title[:30],
            ncols=100,
            position=0,
            leave=True,
        )
    else:
        iterator = enumerate(selected, 1)

    try:
        with logging_redirect_tqdm():
            for idx, ch in iterator:
                label = format_chapter_label(ch["num"])
                ch_dir = manga_dir / f"Chapter_{label}"
                cbz_path = manga_dir / f"{safe_title} - Cap {label}.cbz"

                # Progresso reflete capítulos já concluídos (idx-1). A barra se
                # move assim que o capítulo começa, dando feedback imediato.
                reporter.write(
                    "downloading",
                    progress=round((idx - 1) / total_sel * 100),
                    current_chapter=idx,
                    total_chapters=total_sel,
                    chapter_num=ch["num"],
                    done=stats.chapters_done,
                    skipped=stats.chapters_skipped,
                    failed=stats.chapters_failed,
                )

                if args.format == "cbz" and cbz_path.exists():
                    stats.chapters_skipped += 1
                    log.debug("Cap %s já existe, pulando", label)
                    continue

                ch_start = time.monotonic()
                log.info("[%d/%d] Cap %s: %s",
                         idx, total_sel, label, _short_url(ch["url"]))

                try:
                    images = fetch_chapter_images(
                        session, ch["url"], args.url,
                        rate_limiter=rate_limiter, guard=guard, stats=stats,
                        max_retries=args.max_retries,
                    )
                except Exception as e:
                    log.error("  ✗ falha listando imagens do cap %s: %s", label, e)
                    failed.append({"num": ch["num"], "url": ch["url"], "reason": str(e)[:60]})
                    stats.chapters_failed += 1

                    if guard.should_trip():
                        guard.trip()
                    continue

                if not images:
                    log.warning("  ✗ cap %s sem imagens, pulando", label)
                    failed.append({"num": ch["num"], "url": ch["url"], "reason": "sem imagens"})
                    stats.chapters_failed += 1
                    continue

                ch_dir.mkdir(parents=True, exist_ok=True)
                ok, fail = download_chapter_images(
                    session, images, ch["url"], ch_dir,
                    rate_limiter=rate_limiter, guard=guard, stats=stats,
                    workers=args.image_workers,
                    progress_label=f"Cap {label}",
                )

                elapsed = time.monotonic() - ch_start
                log.info("  ✓ %d/%d imagens em %.1fs (%.0f KB/s)",
                         ok, ok + fail, elapsed,
                         (stats.bytes_total / 1024) / max(stats.elapsed(), 0.1))

                if fail == 0 and args.format == "cbz":
                    make_cbz(ch_dir, cbz_path)
                    shutil.rmtree(ch_dir, ignore_errors=True)
                    stats.chapters_done += 1
                elif fail == 0:
                    stats.chapters_done += 1
                else:
                    failed.append({"num": ch["num"], "url": ch["url"],
                                   "reason": f"{fail} imagens falharam"})
                    stats.chapters_failed += 1

                if not args.no_behavioral_pause:
                    behavioral_pause_if_needed(idx, total_sel)

    except KeyboardInterrupt:
        log.warning("\n⚠  Interrompido pelo usuário (Ctrl+C)")
        reporter.write(
            "cancelled",
            message="interrompido pelo usuário",
            done=stats.chapters_done,
            output_path=str(manga_dir),
        )

    log.info(stats.render())

    if failed:
        log.warning("%d capítulos com problema:", len(failed))
        for f in failed:
            log.warning("  - Cap %s: %s", f["num"], f["reason"])
        nums = ",".join(
            str(int(f["num"]) if f["num"] == int(f["num"]) else f["num"])
            for f in failed
        )
        log.warning("\nRetry só dos que falharam:\n  --chapters %s", nums)

        reporter.write(
            "error",
            message=f"{len(failed)} capítulos falharam",
            progress=100,
            output_path=str(manga_dir),
            done=stats.chapters_done,
            skipped=stats.chapters_skipped,
            failed=stats.chapters_failed,
            failed_chapters=[
                (int(f["num"]) if f["num"] == int(f["num"]) else f["num"])
                for f in failed
            ],
        )
    else:
        reporter.write(
            "done",
            progress=100,
            output_path=str(manga_dir),
            done=stats.chapters_done,
            skipped=stats.chapters_skipped,
        )

    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
