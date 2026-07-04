#!/usr/bin/env python3
"""
centralnovel_dl.py — v3
Downloader de PDFs de capítulos para centralnovel.com.

NOVIDADE (v3): suporte a --status-file. Escreve o progresso em um arquivo JSON
de forma atômica, pra que um front-end externo (ex: o app Godot "Tsundoku")
faça polling e acompanhe o download em tempo real, sem precisar parsear stdout.
Sem --status-file, o script se comporta exatamente como a v2.

IMPORTANTE (desde a v2): o botão "PDF" do site não serve um arquivo pronto.
A URL <capitulo>/pdf/ carrega uma página que usa html2pdf.js (html2canvas +
jsPDF) pra montar o PDF DENTRO DO NAVEGADOR e disparar o download via JS.
Um GET simples (requests/curl_cffi) só recebe o HTML da casca, não o binário —
por isso agora usamos Playwright (Chromium headless de verdade) só pra essa
etapa. Listagem de capítulos continua via requests/curl_cffi, que funciona
normalmente (é HTML estático, sem JS envolvido).

Requisitos:
  pip install requests beautifulsoup4 playwright
  playwright install chromium
Opcionais (recomendados):
  pip install curl_cffi tqdm

Uso:
  python centralnovel_dl.py "https://centralnovel.com/series/shadow-slave-20230928/" --probe
  python centralnovel_dl.py "https://centralnovel.com/series/shadow-slave-20230928/" --chapters 1-50
  python centralnovel_dl.py "https://centralnovel.com/series/shadow-slave-20230928/" --volumes 1-3
  python centralnovel_dl.py "https://centralnovel.com/series/shadow-slave-20230928/" --list

Com acompanhamento de progresso (pro front-end):
  python centralnovel_dl.py "<url>" --chapters 1-40 --output downloads \
      --status-file temp/status.json

Depois de baixado, os PDFs ficam em ./downloads/<Nome da Novel>/Cap 0001.pdf
etc. — prontos pra você jogar no chapterbind e mesclar em EPUB/CBZ/PDF único.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

# ============================================================================ #
# Dependências
# ============================================================================ #
try:
    from bs4 import BeautifulSoup
    import requests
except ImportError as e:
    sys.exit(f"Faltando dependência: {e.name}. Instale: pip install requests beautifulsoup4")

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
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

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
IMPERSONATE_TARGET = "chrome124"

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

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

PDF_HEADER_OVERRIDES = {
    "Accept": "application/pdf,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}

CHAPTER_NUM_PATTERNS = (
    re.compile(r"cap[íi]tulo[\s\-_]*(\d+(?:[.,]\d+)?)", re.I),
    re.compile(r"chapter[\s\-_]*(\d+(?:[.,]\d+)?)", re.I),
    re.compile(r"cap[\s\-_]*(\d+(?:[.,]\d+)?)", re.I),
)

# Padrão do Madara pt-BR: "Vol. 10 Cap. 2581", "Volume 2 Capítulo 4" etc.
# Volume é opcional — várias novels não têm volume (só "Cap. 1156").
VOLUME_PATTERN = re.compile(r"vol(?:ume)?[.\s]*(\d+(?:[.,]\d+)?)", re.I)


# ============================================================================ #
# Logger
# ============================================================================ #
log = logging.getLogger("centralnovel_dl")


def setup_logging(verbose: bool, log_file: Optional[str]) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    log.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s"))
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
            self.consecutive = 0


# ============================================================================ #
# Stats
# ============================================================================ #
@dataclass
class Stats:
    chapters_done: int = 0
    chapters_failed: int = 0
    chapters_skipped: int = 0
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
            f"  Total baixado:   {self.bytes_total / 1024 / 1024:.1f} MB\n"
            f"  Velocidade:      {self.speed_mb():.2f} MB/s\n"
            f"  Retries:         {self.retries}\n"
            f"{'─' * 60}"
        )


# ============================================================================ #
# Session factory
# ============================================================================ #
def make_session():
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
    try:
        p = urlparse(url)
        return p.path.rsplit("/", 1)[-1] or p.netloc
    except Exception:
        return url[-40:]


# ============================================================================ #
# Scraping (Madara)
# ============================================================================ #
def fetch_series_page(session, series_url: str, **kw) -> tuple[str, str]:
    """Retorna (título, html_da_página) da página /series/<slug>/."""
    r = http_request(session, "GET", series_url, timeout=30, **kw)
    soup = BeautifulSoup(r.text, "html.parser")

    title = None
    for sel in ("div.post-title h1", "div.post-title h3", ".manga-title h1", "h1"):
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            break
    title = title or "novel"

    log.debug("Página da série: title=%r", title)
    return title, r.text


def parse_chapter_html(html: str):
    """Extrai capítulos da página da série, incluindo o link direto do PDF
    (já vem pronto no HTML, em .epl-pdf .dlpdf) e o volume (via os acordeões
    .ts-chl-collapsible que envolvem cada bloco de capítulos)."""
    soup = BeautifulSoup(html, "html.parser")
    chapters = []

    # Cada volume é um acordeão .ts-chl-collapsible seguido do bloco
    # .ts-chl-collapsible-content com os capítulos daquele volume.
    vol_blocks = soup.select(".ts-chl-collapsible")
    if vol_blocks:
        for vol_header in vol_blocks:
            vol_text = vol_header.get_text(" ", strip=True)
            vol_num = extract_volume(vol_text)

            content = vol_header.find_next_sibling(class_="ts-chl-collapsible-content")
            if content is None:
                continue

            for li in content.select(".eplister ul li[data-id], li[data-id]"):
                chapters.append(_parse_chapter_li(li, vol_num))

    # Fallback: sem estrutura de volumes reconhecida, pega todos os li[data-id]
    # da página inteira (novels sem volume, ou HTML fora do padrão esperado).
    if not chapters:
        for li in soup.select(".eplister ul li[data-id], li[data-id]"):
            chapters.append(_parse_chapter_li(li, None))

    chapters = [c for c in chapters if c is not None]
    chapters.sort(key=lambda c: c["num"])
    return chapters


def _parse_chapter_li(li, vol_num: Optional[float]) -> Optional[dict]:
    a = li.select_one("a[href]")
    if a is None:
        return None
    href = (a.get("href") or "").strip()
    if not href or "javascript" in href.lower():
        return None

    num_el = li.select_one(".epl-num")
    title_el = li.select_one(".epl-title")
    num_text = num_el.get_text(" ", strip=True) if num_el else a.get_text(" ", strip=True)
    title_text = title_el.get_text(" ", strip=True) if title_el else ""

    # .epl-num vem tipo "Vol. 26 Cap. 11 [Fim]" — usamos pra número e, se o
    # volume não veio do acordeão pai, tentamos extrair daqui também.
    vol = vol_num if vol_num is not None else extract_volume(num_text)

    pdf_a = li.select_one(".epl-pdf .dlpdf[href], .epl-pdf a[href]")
    pdf_url = (pdf_a.get("href") or "").strip() if pdf_a else None

    return {
        "num": extract_chapter_number(num_text, href),
        "vol": vol,
        "title": title_text or num_text,
        "url": href.rstrip("/"),
        "pdf_url": pdf_url,  # link direto pro PDF, já pronto no HTML
        "is_special": _looks_like_special_chapter(title_text, num_text),
    }


def _looks_like_special_chapter(title_text: str, num_text: str) -> bool:
    """Sinaliza capítulos que costumam ser ilustrações/artes em vez de texto
    corrido (ex: 'Coleção: Design de Personagens'), pra avisar no log — o
    download do PDF funciona igual, é só um heads-up de conteúdo."""
    markers = ("coleção", "colecao", "design de personagem", "ilustraç", "ilustrac", "artbook")
    combined = f"{title_text} {num_text}".lower()
    return any(m in combined for m in markers)


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


def extract_volume(text: str) -> Optional[float]:
    """Extrai o número do volume de um texto (cabeçalho do acordeão ou .epl-num).
    Retorna None se a novel não organiza capítulos em volumes."""
    m = VOLUME_PATTERN.search(text)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


def fetch_chapter_list(session, series_url: str, series_html: str, **kw):
    """A página da série do Central Novel já traz o HTML completo de todos os
    volumes/capítulos (confirmado: não há endpoint AJAX nem API REST pública) —
    então só fazemos parse direto do HTML, sem tentar rotas extras."""
    chapters = parse_chapter_html(series_html)
    log.debug("Capítulos obtidos do HTML da página (%d)", len(chapters))
    return chapters


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


def filter_by_volume(volume_str: str, all_chapters: list[dict]) -> list[dict]:
    """Filtra por volume (ex: '1', '1,3', '1-5'). Capítulos sem volume (vol=None)
    nunca entram nesse filtro — só aparecem se --volumes não for usado."""
    if not volume_str or volume_str.lower() == "all":
        return all_chapters

    wanted: set[float] = set()
    ranges: list[tuple[float, float]] = []
    for part in volume_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a_s, b_s = part.split("-", 1)
            a = float(a_s) if a_s else float("-inf")
            b = float(b_s) if b_s else float("inf")
            ranges.append((a, b))
        else:
            wanted.add(float(part))

    def matches(c: dict) -> bool:
        if c["vol"] is None:
            return False
        if c["vol"] in wanted:
            return True
        return any(a <= c["vol"] <= b for a, b in ranges)

    return [c for c in all_chapters if matches(c)]


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip().rstrip(".")[:180]


def format_chapter_label(num: float) -> str:
    if num == int(num):
        return f"{int(num):04d}"
    return f"{num:07.2f}"


# ============================================================================ #
# Pausas comportamentais
# ============================================================================ #
def behavioral_pause_if_needed(idx: int, total: int) -> None:
    if idx == 0 or idx >= total:
        return
    if idx % 25 == 0 and random.random() < 0.85:
        pause = random.uniform(30.0, 90.0)
        log.info("⏸  Pausa comportamental: %.0fs (cap %d/%d)", pause, idx, total)
        time.sleep(pause)


# ============================================================================ #
# Download do PDF do capítulo (via Playwright — o PDF é gerado no browser)
# ============================================================================ #
class CooldownError(Exception):
    """O site pediu pra esperar antes de baixar de novo (rate limit / cooldown).
    seconds é o tempo sugerido pelo site, ou None se não deu pra extrair."""

    def __init__(self, seconds: Optional[float], where: str):
        self.seconds = seconds
        self.where = where
        msg = (
            f"cooldown do site (~{seconds:.0f}s sugeridos): {where}"
            if seconds is not None
            else f"rate limit do site (sem tempo explícito): {where}"
        )
        super().__init__(msg)


# Frases que o site costuma mostrar quando você baixa rápido demais.
_COOLDOWN_TIME_PATTERNS = (
    re.compile(r"aguarde\s+(\d+)\s*segundos?", re.I),
    re.compile(r"espere\s+(\d+)\s*segundos?", re.I),
    re.compile(r"wait\s+(\d+)\s*seconds?", re.I),
    re.compile(r"(\d+)\s*segundos?\s+(?:para|antes)", re.I),
)

_RATE_LIMIT_MARKERS = (
    "aguarde", "espere", "muitas requisi", "too many", "rate limit",
    "tente novamente", "try again", "limite de download", "slow down",
    "aguardar", "cooldown", "please wait", "wait a", "seconds before",
)


def _detect_cooldown_seconds(text: str) -> Optional[float]:
    """Tenta extrair um número de segundos de espera do texto da página."""
    if not text:
        return None
    for pat in _COOLDOWN_TIME_PATTERNS:
        m = pat.search(text)
        if m:
            return float(m.group(1))
    return None


def _looks_like_rate_limit(text: str) -> bool:
    """Heurística: a página parece uma mensagem de rate limit, mesmo sem número."""
    if not text:
        return False
    low = text.lower()
    # Evita falso positivo com páginas enormes de conteúdo — só considera se o
    # texto for curto (mensagem de aviso, não um capítulo inteiro renderizado).
    if len(low) > 2000:
        return False
    return any(m in low for m in _RATE_LIMIT_MARKERS)


def download_chapter_pdf_pw(
    pw_context,
    chapter_url: str,
    dest: Path,
    *,
    pdf_url: Optional[str] = None,
    guard: AdaptiveGuard,
    stats: Stats,
    timeout_ms: int = 45_000,
) -> int:
    """Baixa o PDF do capítulo abrindo <chapter_url>/pdf/ num browser real,
    deixando o html2pdf.js rodar (ele monta o PDF no client-side via
    html2canvas + jsPDF e dispara um download por JS) e capturando esse
    download. Retorna bytes escritos.

    Se o download não disparar, tenta ler o texto visível da página pra
    distinguir um cooldown do site ("aguarde X segundos") de uma falha real
    do html2pdf.js — isso é sinalizado via CooldownError pra que o chamador
    saiba que vale a pena esperar e tentar de novo.
    """
    url = pdf_url or (chapter_url.rstrip("/") + "/pdf/")

    page = pw_context.new_page()
    try:
        try:
            with page.expect_download(timeout=timeout_ms) as download_info:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            download = download_info.value
            download.save_as(str(dest))
        except PlaywrightTimeoutError as e:
            # Download não disparou — investiga o texto da página pra ver se é
            # cooldown/rate limit (que vale retry) ou falha genérica.
            body_text = ""
            try:
                body_text = (page.inner_text("body", timeout=3000) or "").strip()
            except Exception:
                pass

            cooldown_secs = _detect_cooldown_seconds(body_text)
            if cooldown_secs is not None:
                raise CooldownError(cooldown_secs, _short_url(url)) from e
            if _looks_like_rate_limit(body_text):
                raise CooldownError(None, _short_url(url)) from e

            raise ValueError(
                f"nenhum download disparado em {timeout_ms/1000:.0f}s "
                f"(html2pdf.js pode ter falhado nesse capítulo): {_short_url(url)}"
            ) from e
    finally:
        page.close()

    written = dest.stat().st_size if dest.exists() else 0

    # Mesma validação de antes: confere a assinatura real do PDF. Aqui é
    # cinto e suspensório — se o download vier corrompido/incompleto por
    # algum motivo, detectamos antes de dar como sucesso.
    if written == 0:
        raise ValueError(f"download veio vazio: {_short_url(url)}")

    with open(dest, "rb") as f:
        head = f.read(8)
    if not head.lstrip().startswith(b"%PDF-"):
        preview = head[:8]
        try:
            dest.unlink()
        except Exception:
            pass
        raise ValueError(f"arquivo baixado não é um PDF válido (head={preview!r}): {_short_url(url)}")

    guard.record_success()
    stats.bytes_total += written
    return written


# ============================================================================ #
# Main
# ============================================================================ #
def main() -> int:
    p = argparse.ArgumentParser(
        description="Downloader de PDFs de capítulos para centralnovel.com (Madara)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", help="URL da série (ex: https://centralnovel.com/series/<slug>/)")
    p.add_argument("--chapters", default="all", help="Filtro: 'all', '1-10', '1,3,5', '5-', '-10'")
    p.add_argument("--volumes", default=None,
                   help="Filtro por volume: '1', '1,3', '1-5'. Aplicado junto com --chapters "
                        "(interseção). Novels sem volume no site não são pegas por este filtro.")
    p.add_argument("--output", default="./downloads", help="Pasta de saída (padrão: ./downloads)")
    p.add_argument("--status-file", default=None,
                   help="Escreve progresso em JSON neste caminho (pra front-ends "
                        "tipo o app Godot fazerem polling). Escrita atômica, opcional.")
    p.add_argument("--list", action="store_true", help="Só lista capítulos, não baixa")
    p.add_argument("--probe", action="store_true",
                   help="Modo teste: baixa só o primeiro capítulo do filtro pra validar setup")
    p.add_argument("--rate", type=float, default=1.0, help="Requests/segundo sustentado (padrão: 1.0)")
    p.add_argument("--burst", type=int, default=3, help="Tokens iniciais do bucket (padrão: 3)")
    p.add_argument("--max-retries", type=int, default=5, help="Tentativas máximas por request (padrão: 5)")
    p.add_argument("--no-behavioral-pause", action="store_true",
                   help="Desativa pausas longas a cada ~25 caps")
    p.add_argument("--pdf-timeout", type=float, default=45.0,
                   help="Timeout em segundos pra esperar o html2pdf.js gerar o download (padrão: 45)")
    p.add_argument("--pdf-retries", type=int, default=4,
                   help="Tentativas por capítulo antes de desistir (padrão: 4). "
                        "Cooldowns do site respeitam o tempo pedido; falhas genéricas usam backoff.")
    p.add_argument("--cooldown-wait", type=float, default=60.0,
                   help="Espera (s) quando o site pede cooldown sem dizer o tempo exato (padrão: 60)")
    p.add_argument("--headed", action="store_true",
                   help="Roda o browser com interface visível (útil pra debugar visualmente)")
    p.add_argument("--verbose", "-v", action="store_true", help="Loga retries e detalhes")
    p.add_argument("--log", metavar="FILE", help="Salva log completo em arquivo")
    args = p.parse_args()

    setup_logging(args.verbose, args.log)

    reporter = StatusReporter(args.status_file)

    if not HAS_TQDM:
        log.info("dica: pip install tqdm  → barra de progresso melhor")
    if _curl_cffi is None:
        log.warning("curl_cffi NÃO instalado — recomendado pra stealth: pip install curl_cffi")
    if not HAS_PLAYWRIGHT:
        log.error(
            "Playwright NÃO instalado. O PDF do Central Novel é gerado por JS no "
            "navegador (html2pdf.js) — precisa de um browser real pra baixar.\n"
            "Instale com:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )
        reporter.write("error", message="Playwright não instalado")
        return 1

    rate_limiter = RateLimiter(rate=args.rate, burst=args.burst)
    guard = AdaptiveGuard(fail_threshold=5, breaker_cooldown=300.0)
    stats = Stats()
    session = make_session()

    log.info("→ %s", args.url)
    reporter.write("starting", url=args.url)
    try:
        title, series_html = fetch_series_page(
            session, args.url,
            rate_limiter=rate_limiter, guard=guard, stats=stats,
            max_retries=args.max_retries,
        )
    except Exception as e:
        log.error("falha carregando página da série: %s", e)
        reporter.write("error", message=f"falha carregando página da série: {e}")
        return 1
    log.info("Título: %s", title)

    try:
        chapters = fetch_chapter_list(
            session, args.url, series_html,
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
    if args.volumes:
        before = len(selected)
        selected = filter_by_volume(args.volumes, selected)
        log.info("Filtro de volume '%s': %d → %d capítulos", args.volumes, before, len(selected))

    if not selected:
        log.error("Nenhum capítulo dentro do filtro: --chapters %s --volumes %s",
                   args.chapters, args.volumes)
        reporter.write("error", message="nenhum capítulo dentro do filtro")
        return 1

    if args.probe:
        selected = selected[:1]
        log.info("MODO PROBE: baixando só o primeiro do filtro pra validar setup")

    log.info("%d capítulos selecionados", len(selected))

    if args.list:
        for c in selected:
            vol_tag = f"Vol.{c['vol']:g} " if c["vol"] is not None else ""
            print(f"  - {vol_tag}Cap {c['num']:>6}: {c['title']}")
        return 0

    reporter.write(
        "preparing",
        title=title,
        total_chapters=len(selected),
        progress=0,
    )

    safe_title = sanitize_filename(title)
    novel_dir = Path(args.output) / safe_title
    novel_dir.mkdir(parents=True, exist_ok=True)

    failed: list[dict] = []
    total_sel = len(selected)

    if HAS_TQDM:
        iterator_factory = lambda: tqdm(
            enumerate(selected, 1),
            total=total_sel,
            unit="cap",
            desc=safe_title[:30],
            ncols=100,
            position=0,
            leave=True,
        )
    else:
        iterator_factory = lambda: enumerate(selected, 1)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            pw_context = browser.new_context(accept_downloads=True)

            try:
                with logging_redirect_tqdm():
                    for idx, ch in iterator_factory():
                        label = format_chapter_label(ch["num"])

                        # Progresso reflete capítulos já concluídos (idx-1).
                        # A barra se move assim que o capítulo começa, dando
                        # feedback imediato ao front-end.
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

                        vol_prefix = f"Vol {ch['vol']:g} - " if ch["vol"] is not None else ""
                        pdf_path = novel_dir / f"{safe_title} - {vol_prefix}Cap {label}.pdf"

                        if pdf_path.exists() and pdf_path.stat().st_size > 0:
                            stats.chapters_skipped += 1
                            log.debug("Cap %s já existe, pulando", label)
                            continue

                        # Rate limiting continua aplicado mesmo no browser,
                        # pra não martelar o servidor de requisições de página.
                        rate_limiter.acquire()
                        extra = guard.extra_delay()
                        if extra > 0:
                            time.sleep(extra)

                        ch_start = time.monotonic()
                        log.info("[%d/%d] Cap %s: %s",
                                 idx, total_sel, label, _short_url(ch["url"]))
                        if ch.get("is_special"):
                            log.info("  ℹ capítulo parece ser conteúdo especial (ilustração/artbook), não texto corrido")

                        written = None
                        last_error = None
                        for attempt in range(1, args.pdf_retries + 1):
                            try:
                                written = download_chapter_pdf_pw(
                                    pw_context, ch["url"], pdf_path,
                                    pdf_url=ch.get("pdf_url"),
                                    guard=guard, stats=stats,
                                    timeout_ms=int(args.pdf_timeout * 1000),
                                )
                                break  # sucesso

                            except CooldownError as e:
                                last_error = e
                                # O site pediu pra esperar. Usa o tempo sugerido
                                # (com folga) ou o --cooldown-wait como base.
                                base = e.seconds if e.seconds is not None else args.cooldown_wait
                                wait = base + random.uniform(2.0, 6.0)
                                if attempt < args.pdf_retries:
                                    # Estado de cooldown é ouro pro front-end: em
                                    # vez de parecer travado, mostra o contador.
                                    reporter.write(
                                        "cooldown",
                                        progress=round((idx - 1) / total_sel * 100),
                                        current_chapter=idx,
                                        total_chapters=total_sel,
                                        chapter_num=ch["num"],
                                        wait_seconds=round(wait),
                                        attempt=attempt,
                                        max_attempts=args.pdf_retries,
                                    )
                                    log.warning(
                                        "  ⏳ cooldown do site no cap %s — esperando %.0fs "
                                        "(tentativa %d/%d)",
                                        label, wait, attempt, args.pdf_retries,
                                    )
                                    time.sleep(wait)
                                else:
                                    log.error("  ✗ cap %s ainda em cooldown após %d tentativas",
                                              label, args.pdf_retries)

                            except Exception as e:
                                last_error = e
                                # Falha genérica (timeout do html2pdf, etc.):
                                # backoff exponencial mais curto.
                                wait = min(60.0, (2 ** (attempt - 1)) * 5.0) + random.uniform(0, 3.0)
                                if attempt < args.pdf_retries:
                                    log.warning(
                                        "  ↻ falha no cap %s (%s) — retry %d/%d em %.0fs",
                                        label, str(e)[:60], attempt, args.pdf_retries, wait,
                                    )
                                    time.sleep(wait)
                                else:
                                    log.error("  ✗ cap %s falhou após %d tentativas",
                                              label, args.pdf_retries)

                        if written is not None:
                            elapsed = time.monotonic() - ch_start
                            log.info("  ✓ %.1f KB em %.1fs", written / 1024, elapsed)
                            stats.chapters_done += 1
                        else:
                            log.error("  ✗ falha baixando PDF do cap %s: %s", label, last_error)
                            failed.append({"num": ch["num"], "url": ch["url"],
                                           "reason": str(last_error)[:80]})
                            stats.chapters_failed += 1
                            guard.record_failure()
                            try:
                                if pdf_path.exists():
                                    pdf_path.unlink()
                            except Exception:
                                pass

                            if guard.should_trip():
                                guard.trip()
                            continue

                        if not args.no_behavioral_pause:
                            behavioral_pause_if_needed(idx, total_sel)
            finally:
                pw_context.close()
                browser.close()

    except KeyboardInterrupt:
        log.warning("\n⚠  Interrompido pelo usuário (Ctrl+C)")
        reporter.write(
            "cancelled",
            message="interrompido pelo usuário",
            done=stats.chapters_done,
            output_path=str(novel_dir),
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
            output_path=str(novel_dir),
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
            output_path=str(novel_dir),
            done=stats.chapters_done,
            skipped=stats.chapters_skipped,
        )

    log.info("\nPDFs prontos em: %s", novel_dir)
    log.info("Dica: jogue essa pasta no chapterbind pra mesclar em EPUB/CBZ/PDF único.")

    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
