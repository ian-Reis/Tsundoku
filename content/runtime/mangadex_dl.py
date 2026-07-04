#!/usr/bin/env python3
"""
mangadex_dl.py — v1
Downloader de mangá para MangaDex, usando a API oficial (api.mangadex.org).

Diferente dos outros scripts do Tsundoku (centralnovel_dl, mangalivre_dl), este
NÃO precisa de curl_cffi nem Playwright: o MangaDex tem API REST pública e
documentada, sem Cloudflare nem JS. Por isso o script é bem mais simples — só
requests batendo nos endpoints oficiais, com um rate limiter respeitoso.

Fluxo da API (3 passos):
  1. GET /manga/{id}/feed          → lista os capítulos (com filtro de idioma)
  2. GET /at-home/server/{chapId}  → obtém baseUrl + hash + lista de páginas
  3. GET {baseUrl}/data/{hash}/{page}  → baixa cada imagem

Sobre rate limit (importante):
  - Limite global de ~5 req/s por IP em api.mangadex.org. Estourar gera 429 e,
    se insistir, ban temporário de IP (403). O rate limiter aqui é conservador.
  - As imagens vêm de servidores MangaDex@Home (baseUrl != mangadex.org), que
    têm limites próprios e mais estritos — por isso baixamos as páginas em série
    (ou com poucos workers), não em rajada.
  - Desde 2026, LEITURA DE CAPÍTULOS PARA CONVIDADOS (sem login) é limitada a
    ~10 capítulos/dia como medida anti-scraping. Para downloads grandes você
    pode precisar de autenticação (não implementada aqui ainda) ou baixar em
    lotes pequenos. O script avisa se receber 403/429 persistente.

Requisitos:
  pip install requests
Opcionais (recomendados):
  pip install tqdm

Uso:
  python mangadex_dl.py "https://mangadex.org/title/<uuid>/<slug>" --probe
  python mangadex_dl.py "https://mangadex.org/title/<uuid>/..." --chapters 1-40 --lang pt-br
  python mangadex_dl.py "<url_ou_uuid>" --list --lang en

Com acompanhamento de progresso (pro front-end Godot):
  python mangadex_dl.py "<url>" --chapters 1-40 --output downloads \
      --status-file temp/status.json --lang pt-br
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# ============================================================================ #
# Encoding do console (Windows)
# ============================================================================ #
# O console do Windows usa cp1252 por padrão, que quebra ao imprimir caracteres
# especiais (títulos em japonês, emojis do log, acentos). O UnicodeEncodeError
# resultante pode ser confundido com falha de rede. Forçar UTF-8 resolve na raiz.
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
    import requests
except ImportError as e:
    sys.exit(f"Faltando dependência: {e.name}. Instale: pip install requests")

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
API_BASE = "https://api.mangadex.org"

# User-Agent identificável é boa prática com APIs oficiais (ao contrário do
# scraping, onde a gente se disfarça de browser). Ajuda o MangaDex a saber
# quem está batendo, e é o comportamento respeitoso esperado.
USER_AGENT = "tsundoku-mangadex-dl/1.0 (+https://github.com/seu-usuario/tsundoku)"

RETRY_STATUS = {429, 500, 502, 503, 504}

# Extensões de imagem válidas que a API retorna
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# UUID v4 (formato dos IDs do MangaDex)
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


# ============================================================================ #
# Logger
# ============================================================================ #
log = logging.getLogger("mangadex_dl")


def setup_logging(verbose: bool, log_file: Optional[str]) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    console.encoding = "utf-8"
    console.errors = "replace"
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
    """Escreve o progresso num arquivo JSON de forma atômica (.tmp + os.replace,
    atômico no Windows e Linux). No-op se status_file for None. Nunca lança
    exceção pra cima — um erro de I/O no status não pode derrubar o download."""

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
    """Limita a taxa média de requests com tolerância a bursts.
    Default conservador (rate=4, burst=4) fica abaixo do limite global de ~5/s
    do MangaDex, deixando folga pra não disparar o 429."""

    def __init__(self, rate: float = 4.0, burst: int = 4):
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
# HTTP com retry
# ============================================================================ #
def make_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    return sess


def http_get(
    session: requests.Session,
    url: str,
    *,
    rate_limiter: Optional[RateLimiter] = None,
    stats: Optional[Stats] = None,
    max_retries: int = 5,
    is_api: bool = True,
    **kwargs,
) -> requests.Response:
    """GET com rate limiting + retry/backoff. Respeita Retry-After.

    is_api distingue chamadas à API (api.mangadex.org) de chamadas de imagem
    (servidores MangaDex@Home) — apenas para deixar o log mais claro sobre onde
    um 403/429 aconteceu, já que os dois têm limites independentes.
    """
    last_error: Optional[Exception] = None

    for attempt in range(max_retries):
        if rate_limiter is not None:
            rate_limiter.acquire()

        try:
            r = session.get(url, **kwargs)
            status = r.status_code

            if status == 403 and is_api:
                # 403 na API costuma significar ban temporário por rate limit,
                # OU o limite anti-scraping de convidado (10 caps/dia sem login).
                raise PermissionError(
                    "HTTP 403 do MangaDex — pode ser ban temporário por rate "
                    "limit, ou o limite de 10 capítulos/dia para convidados "
                    "(sem login). Espere um pouco ou baixe em lotes menores."
                )

            if status in RETRY_STATUS:
                last_error = Exception(f"HTTP {status}")
                if stats:
                    stats.retries += 1

                ra = (r.headers.get("Retry-After") or "").strip()
                if ra and ra.isdigit():
                    wait = min(float(ra), 120.0)
                else:
                    wait = (2 ** attempt) + random.uniform(0, 1.0)

                log.warning("HTTP %s em %s, retry %d/%d em %.1fs",
                            status, _short(url), attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue

            r.raise_for_status()
            return r

        except PermissionError:
            raise  # não adianta retry num ban; sobe direto
        except KeyboardInterrupt:
            raise
        except Exception as e:
            last_error = e
            if stats:
                stats.retries += 1
            if attempt == max_retries - 1:
                break
            wait = (2 ** attempt) + random.uniform(0, 1.0)
            log.warning("%s em %s, retry %d/%d em %.1fs",
                        type(e).__name__, _short(url), attempt + 1, max_retries, wait)
            time.sleep(wait)

    raise last_error if last_error else RuntimeError("falha sem causa identificável")


def _short(url: str) -> str:
    try:
        p = urlparse(url)
        return (p.path.rsplit("/", 1)[-1] or p.netloc)[:40]
    except Exception:
        return url[-40:]


# ============================================================================ #
# API MangaDex
# ============================================================================ #
def extract_manga_id(url_or_id: str) -> Optional[str]:
    """Extrai o UUID do mangá de uma URL do MangaDex ou aceita o UUID direto."""
    m = UUID_RE.search(url_or_id)
    return m.group(0) if m else None


def fetch_manga_title(session, manga_id: str, **kw) -> str:
    """Busca o título do mangá. Tenta inglês, depois romaji, depois qualquer um."""
    url = f"{API_BASE}/manga/{manga_id}"
    r = http_get(session, url, **kw)
    attrs = r.json().get("data", {}).get("attributes", {})
    titles = attrs.get("title", {})
    alt = attrs.get("altTitles", [])

    for key in ("en", "ja-ro", "ja"):
        if titles.get(key):
            return titles[key]
    if titles:
        return next(iter(titles.values()))
    for d in alt:
        for v in d.values():
            return v
    return "manga"


def fetch_chapter_feed(session, manga_id: str, lang: str, *, rate_limiter, stats, **kw):
    """Lista todos os capítulos do mangá no idioma pedido, paginando o feed.

    O feed aceita no máximo 500 itens por request (limite dos endpoints de feed)
    e offset+limit não pode passar de 10.000. Paginamos em blocos de 500.
    """
    chapters = []
    limit = 500
    offset = 0

    while True:
        params = {
            "translatedLanguage[]": lang,
            "limit": limit,
            "offset": offset,
            "order[chapter]": "asc",
            "includeExternalUrl": 0,  # pula capítulos que só existem em site externo
            "contentRating[]": ["safe", "suggestive", "erotica", "pornographic"],
        }
        url = f"{API_BASE}/manga/{manga_id}/feed"
        r = http_get(session, url, params=params,
                     rate_limiter=rate_limiter, stats=stats, **kw)
        payload = r.json()

        data = payload.get("data", [])
        for ch in data:
            attrs = ch.get("attributes", {})
            # Pula capítulos sem páginas (pages=0) — externos ou placeholders
            if attrs.get("pages", 0) <= 0:
                continue
            num_raw = attrs.get("chapter")
            chapters.append({
                "id": ch["id"],
                "num": _parse_num(num_raw),
                "num_raw": num_raw,
                "vol": attrs.get("volume"),
                "title": attrs.get("title") or "",
                "pages": attrs.get("pages", 0),
            })

        total = payload.get("total", 0)
        offset += limit
        if offset >= total or not data:
            break

    # Dedup por número: o mesmo capítulo pode ter várias versões (grupos de scan
    # diferentes). Mantém a primeira ocorrência de cada número.
    seen = set()
    deduped = []
    for c in sorted(chapters, key=lambda c: (c["num"] if c["num"] is not None else 1e9)):
        key = c["num_raw"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    return deduped


def fetch_at_home(session, chapter_id: str, *, rate_limiter, stats, **kw) -> dict:
    """Obtém baseUrl + hash + lista de páginas de um capítulo.
    O baseUrl é otimizado geograficamente e válido por ~15 min."""
    url = f"{API_BASE}/at-home/server/{chapter_id}"
    r = http_get(session, url, rate_limiter=rate_limiter, stats=stats, **kw)
    j = r.json()
    return {
        "base_url": j["baseUrl"],
        "hash": j["chapter"]["hash"],
        "data": j["chapter"]["data"],            # páginas em qualidade original
        "data_saver": j["chapter"]["dataSaver"], # páginas comprimidas
    }


def _parse_num(raw) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "."))
    except (ValueError, TypeError):
        return None


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
            selected.extend(c for c in all_chapters
                            if c["num"] is not None and a <= c["num"] <= b)
        else:
            n = float(part)
            selected.extend(c for c in all_chapters if c["num"] == n)

    seen, result = set(), []
    for c in selected:
        if c["id"] not in seen:
            seen.add(c["id"])
            result.append(c)
    return result


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip().rstrip(".")[:180]


def format_chapter_label(num: Optional[float], num_raw) -> str:
    if num is None:
        # Capítulo sem número (oneshot, etc.) — usa o raw sanitizado
        return sanitize_filename(str(num_raw or "oneshot"))
    if num == int(num):
        return f"{int(num):04d}"
    return f"{num:07.2f}"


# ============================================================================ #
# Download de um capítulo
# ============================================================================ #
def download_chapter(
    session,
    chapter: dict,
    out_dir: Path,
    *,
    rate_limiter: RateLimiter,
    stats: Stats,
    data_saver: bool,
    max_retries: int,
) -> tuple[int, int]:
    """Baixa todas as páginas de um capítulo. Retorna (ok, fail).

    As imagens vêm dos servidores MangaDex@Home (não de mangadex.org), com
    limites próprios. Baixamos em série para ser respeitoso — a maioria dos
    capítulos tem 15-40 páginas, então serial é rápido o bastante e evita
    estressar o nó @Home.
    """
    info = fetch_at_home(session, chapter["id"],
                         rate_limiter=rate_limiter, stats=stats, max_retries=max_retries)

    quality = "data-saver" if data_saver else "data"
    pages = info["data_saver"] if data_saver else info["data"]
    base = info["base_url"]
    chash = info["hash"]

    ok = fail = 0
    for idx, page_file in enumerate(pages, 1):
        ext = Path(page_file).suffix.lower()
        if ext not in IMG_EXTS:
            ext = ".jpg"
        dest = out_dir / f"{idx:03d}{ext}"

        if dest.exists() and dest.stat().st_size > 0:
            ok += 1
            continue

        page_url = f"{base}/{quality}/{chash}/{page_file}"
        try:
            r = http_get(session, page_url, stream=True, timeout=60,
                         rate_limiter=rate_limiter, stats=stats,
                         max_retries=max_retries, is_api=False)
            written = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
            stats.bytes_total += written
            ok += 1
        except Exception as e:
            log.warning("  página %d falhou: %s", idx, str(e)[:80])
            try:
                if dest.exists():
                    dest.unlink()
            except Exception:
                pass
            fail += 1

    stats.images_ok += ok
    stats.images_fail += fail
    return ok, fail


def make_cbz(image_dir: Path, cbz_path: Path) -> None:
    images = sorted(p for p in image_dir.iterdir() if p.is_file())
    with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, img in enumerate(images, 1):
            zf.write(img, f"{i:03d}{img.suffix.lower()}")


# ============================================================================ #
# Main
# ============================================================================ #
def main() -> int:
    p = argparse.ArgumentParser(
        description="Downloader de mangá para MangaDex via API oficial",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", help="URL do mangá (mangadex.org/title/<uuid>/...) ou o UUID direto")
    p.add_argument("--chapters", default="all", help="Filtro: 'all', '1-10', '1,3,5', '5-', '-10'")
    p.add_argument("--lang", default="pt-br",
                   help="Código de idioma da tradução (padrão: pt-br). Ex: en, ja, es-la")
    p.add_argument("--output", default="./downloads", help="Pasta de saída (padrão: ./downloads)")
    p.add_argument("--format", choices=["cbz", "folder"], default="cbz")
    p.add_argument("--data-saver", action="store_true",
                   help="Baixa imagens comprimidas (menor, mais rápido, ótimo pro e-reader)")
    p.add_argument("--status-file", default=None,
                   help="Escreve progresso em JSON neste caminho (pra front-ends "
                        "tipo o app Godot fazerem polling). Escrita atômica, opcional.")
    p.add_argument("--list", action="store_true", help="Só lista capítulos, não baixa")
    p.add_argument("--probe", action="store_true",
                   help="Modo teste: baixa só o primeiro capítulo do filtro pra validar setup")
    p.add_argument("--rate", type=float, default=4.0,
                   help="Requests/segundo (padrão: 4.0, abaixo do limite de 5/s do MangaDex)")
    p.add_argument("--burst", type=int, default=4, help="Tokens iniciais do bucket (padrão: 4)")
    p.add_argument("--max-retries", type=int, default=5, help="Tentativas por request (padrão: 5)")
    p.add_argument("--verbose", "-v", action="store_true", help="Loga retries e detalhes")
    p.add_argument("--log", metavar="FILE", help="Salva log completo em arquivo")
    args = p.parse_args()

    setup_logging(args.verbose, args.log)
    reporter = StatusReporter(args.status_file)

    if not HAS_TQDM:
        log.info("dica: pip install tqdm  → barra de progresso melhor")

    manga_id = extract_manga_id(args.url)
    if not manga_id:
        log.error("Não achei um UUID válido em: %s", args.url)
        log.error("Passe a URL completa (mangadex.org/title/<uuid>/...) ou o UUID direto.")
        reporter.write("error", message="UUID do mangá não encontrado na URL")
        return 1

    rate_limiter = RateLimiter(rate=args.rate, burst=args.burst)
    stats = Stats()
    session = make_session()

    log.info("→ MangaDex manga id: %s", manga_id)
    reporter.write("starting", url=args.url, manga_id=manga_id)

    try:
        title = fetch_manga_title(session, manga_id,
                                  rate_limiter=rate_limiter, stats=stats,
                                  max_retries=args.max_retries)
    except PermissionError as e:
        log.error("%s", e)
        reporter.write("error", message=str(e))
        return 1
    except Exception as e:
        log.error("falha buscando dados do mangá: %s", e)
        reporter.write("error", message=f"falha buscando dados do mangá: {e}")
        return 1
    log.info("Título: %s", title)

    try:
        chapters = fetch_chapter_feed(session, manga_id, args.lang,
                                      rate_limiter=rate_limiter, stats=stats,
                                      max_retries=args.max_retries)
    except PermissionError as e:
        log.error("%s", e)
        reporter.write("error", message=str(e))
        return 1
    except Exception as e:
        log.error("falha listando capítulos: %s", e)
        reporter.write("error", message=f"falha listando capítulos: {e}")
        return 1

    if not chapters:
        log.error("Nenhum capítulo encontrado no idioma '%s'. Tente outro --lang "
                  "(ex: en) ou confira se o mangá tem tradução nesse idioma.", args.lang)
        reporter.write("error", message=f"nenhum capítulo no idioma {args.lang}")
        return 1

    nums = [c["num"] for c in chapters if c["num"] is not None]
    if nums:
        log.info("%d capítulos em '%s' (cap %s → %s)",
                 len(chapters), args.lang, min(nums), max(nums))
    else:
        log.info("%d capítulos em '%s'", len(chapters), args.lang)

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
            vol = f"Vol.{c['vol']} " if c["vol"] else ""
            ttl = f" — {c['title']}" if c["title"] else ""
            log.info("  - %sCap %s (%d págs)%s", vol, c["num_raw"], c["pages"], ttl)
        return 0

    reporter.write("preparing", title=title, total_chapters=len(selected), progress=0)

    safe_title = sanitize_filename(title)
    manga_dir = Path(args.output) / safe_title
    manga_dir.mkdir(parents=True, exist_ok=True)

    failed: list[dict] = []
    total_sel = len(selected)

    if HAS_TQDM:
        iterator = tqdm(enumerate(selected, 1), total=total_sel, unit="cap",
                        desc=safe_title[:30], ncols=100, position=0, leave=True)
    else:
        iterator = enumerate(selected, 1)

    try:
        with logging_redirect_tqdm():
            for idx, ch in iterator:
                label = format_chapter_label(ch["num"], ch["num_raw"])
                vol_prefix = f"Vol {ch['vol']} - " if ch["vol"] else ""
                cbz_path = manga_dir / f"{safe_title} - {vol_prefix}Cap {label}.cbz"
                ch_dir = manga_dir / f"Chapter_{label}"

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
                log.info("[%d/%d] Cap %s (%d págs)", idx, total_sel, label, ch["pages"])

                try:
                    ch_dir.mkdir(parents=True, exist_ok=True)
                    ok, fail = download_chapter(
                        session, ch, ch_dir,
                        rate_limiter=rate_limiter, stats=stats,
                        data_saver=args.data_saver, max_retries=args.max_retries,
                    )
                except PermissionError as e:
                    # Ban / limite de convidado — não adianta continuar.
                    log.error("  ✗ %s", e)
                    shutil.rmtree(ch_dir, ignore_errors=True)
                    reporter.write("error", message=str(e), progress=round((idx-1)/total_sel*100),
                                   output_path=str(manga_dir), done=stats.chapters_done)
                    log.info(stats.render())
                    return 1
                except Exception as e:
                    log.error("  ✗ falha no cap %s: %s", label, str(e)[:80])
                    failed.append({"num": ch["num"], "reason": str(e)[:60]})
                    stats.chapters_failed += 1
                    shutil.rmtree(ch_dir, ignore_errors=True)
                    continue

                elapsed = time.monotonic() - ch_start
                log.info("  ✓ %d/%d páginas em %.1fs", ok, ok + fail, elapsed)

                if fail == 0 and args.format == "cbz":
                    make_cbz(ch_dir, cbz_path)
                    shutil.rmtree(ch_dir, ignore_errors=True)
                    stats.chapters_done += 1
                elif fail == 0:
                    stats.chapters_done += 1
                else:
                    failed.append({"num": ch["num"], "reason": f"{fail} páginas falharam"})
                    stats.chapters_failed += 1

    except KeyboardInterrupt:
        log.warning("\n⚠  Interrompido pelo usuário (Ctrl+C)")
        reporter.write("cancelled", message="interrompido pelo usuário",
                       done=stats.chapters_done, output_path=str(manga_dir))

    log.info(stats.render())

    if failed:
        log.warning("%d capítulos com problema:", len(failed))
        for f in failed:
            log.warning("  - Cap %s: %s", f["num"], f["reason"])
        nums_failed = ",".join(
            str(int(f["num"]) if (f["num"] is not None and f["num"] == int(f["num"])) else f["num"])
            for f in failed if f["num"] is not None
        )
        if nums_failed:
            log.warning("\nRetry só dos que falharam:\n  --chapters %s", nums_failed)

        reporter.write(
            "error",
            message=f"{len(failed)} capítulos falharam",
            progress=100,
            output_path=str(manga_dir),
            done=stats.chapters_done,
            skipped=stats.chapters_skipped,
            failed=stats.chapters_failed,
            failed_chapters=[f["num"] for f in failed if f["num"] is not None],
        )
    else:
        reporter.write(
            "done",
            progress=100,
            output_path=str(manga_dir),
            done=stats.chapters_done,
            skipped=stats.chapters_skipped,
        )

    log.info("\nArquivos prontos em: %s", manga_dir)
    log.info("Dica: jogue essa pasta no chapterbind pra mesclar em EPUB/CBZ/PDF único.")

    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
