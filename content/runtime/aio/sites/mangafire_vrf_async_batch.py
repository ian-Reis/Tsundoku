"""Multi-page parallel MangaFire VRF capture (Patchright async API).

What this module owns:
  - One long-lived asyncio event loop on a dedicated thread.
  - One Patchright async-API browser + context (loads the same storage_state
    the sync generator persists).
  - Concurrent page navigations across N pages — captures N chapter VRFs
    in parallel, then writes them into the sync side's shared `_vrf_cache`.

What reads from it:
  - aio-dl.py:_vrf_prefetch_worker_loop, when --mangafire-vrf-parallel > 1,
    submits batches via submit_batch().
  - The captured VRFs flow into sites/mangafire_vrf_simple._vrf_cache via
    populate_vrf_cache(); the sync ensure_vrf() called by the foreground
    download loop hits cache instantly.

Why a separate module: the sync `SimpleMangaFireVRFGenerator` is bound to
its single executor thread (Patchright sync API thread-affinity). Adding
async-API capture would require interleaving sync+async calls on the same
browser, which Playwright/Patchright doesn't support cleanly. Cleanest
isolation: one async loop on its own thread, separate browser instance.
Both sync and async write into one shared cache dict.

Bench evidence (2026-05-09, 8 real chapter IDs, single cf_clearance):
  conc=1 sequential: 16.18s for 8 chapters (~2.0s/chapter)
  conc=4 async batch: 3.11s for 8 chapters (~0.39s/chapter, 5.2x speedup)
  conc=6+: CF rate-limits (homepage redirects on the burst).
The 5.2x is bench-best; production sessions on different IPs may see CF
heuristics trigger sooner. Bounce-detection retries each failing page once
before falling back to the sync sequential path.

Cross-file coupling:
  - sites/mangafire_vrf_simple.py:populate_vrf_cache (callback we use)
  - sites/mangafire_vrf_simple._DEFAULT_STATE_PATH (storage_state we read)
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse


# Reuse storage_state location + UA so cf_clearance fingerprint matches the
# sync generator. Imports done inside the worker to avoid module-load-time
# Patchright import cost when the user never opts into parallel mode.
_BASE_URL = "https://mangafire.to"
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_DESKTOP_VIEWPORT = {"width": 1280, "height": 800}
_LAUNCH_ARGS = [
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
]


def _log(msg: str) -> None:
    import sys as _sys
    print(
        f"[VRF-async pid={os.getpid()} tid={threading.get_ident()} "
        f"{time.strftime('%H:%M:%S')}] {msg}",
        file=_sys.stderr,
    )


class AsyncBatchVRFCapture:
    """Single-instance helper. Owns one async event loop on a dedicated
    thread. submit_batch() schedules a coroutine on that loop and blocks
    the caller until completion."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._browser = None
        self._context = None
        self._playwright_cm = None
        self._init_lock = threading.Lock()
        self._closed = False
        # Spin up the loop thread immediately; browser launch is lazy on
        # first submit_batch (heavy operation, no point paying it if the
        # caller never actually batches).
        self._start_loop_thread()

    # ── Loop-thread management ────────────────────────────────────────────

    def _start_loop_thread(self) -> None:
        loop_ready = threading.Event()

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            loop_ready.set()
            try:
                self._loop.run_forever()
            finally:
                # Best-effort cleanup of remaining tasks before close
                try:
                    pending = asyncio.all_tasks(self._loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        self._loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                except Exception:
                    pass
                self._loop.close()

        self._loop_thread = threading.Thread(
            target=_run, daemon=True, name="VRF-Async-Batch-Loop"
        )
        self._loop_thread.start()
        loop_ready.wait(timeout=5.0)

    def _run_coro(self, coro, timeout: float = 60.0):
        """Submit a coroutine to the loop thread and block until result."""
        if self._loop is None or self._closed:
            raise RuntimeError("AsyncBatchVRFCapture loop is not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # ── Browser lifecycle ─────────────────────────────────────────────────

    async def _ensure_browser(self) -> None:
        """Lazy-launch on first batch. Reuses storage_state from the sync
        generator's cache so cf_clearance is shared (single CF challenge
        across both sync + async sides)."""
        if self._context is not None:
            return

        from patchright.async_api import async_playwright
        # Force lazy-init of the sync generator first so the storage_state
        # exists on disk before we try to load it.
        from sites.mangafire_vrf_simple import (
            _DEFAULT_STATE_PATH,
            _ensure_vrf_singleton_started,
        )
        try:
            _ensure_vrf_singleton_started()
        except Exception as exc:
            _log(f"sync singleton init warning: {exc}")

        self._playwright_cm = async_playwright()
        p = await self._playwright_cm.__aenter__()
        self._browser = await p.chromium.launch(
            headless=True, args=_LAUNCH_ARGS
        )

        ctx_kwargs: Dict[str, object] = {
            "user_agent": _DESKTOP_UA,
            "viewport": _DESKTOP_VIEWPORT,
            "locale": "en-US",
        }
        if os.path.exists(_DEFAULT_STATE_PATH):
            ctx_kwargs["storage_state"] = _DEFAULT_STATE_PATH
        try:
            self._context = await self._browser.new_context(**ctx_kwargs)
        except Exception as exc:
            _log(f"context init with storage_state failed ({exc}); retrying clean")
            ctx_kwargs.pop("storage_state", None)
            self._context = await self._browser.new_context(**ctx_kwargs)

        # Resource filter — only allow document/script/xhr/fetch (mirrors
        # sync side; we don't need stylesheets/images for VRF capture).
        async def _route_handler(route, req):
            try:
                if req.resource_type in {"document", "script", "xhr", "fetch"}:
                    await route.continue_()
                else:
                    await route.abort()
            except Exception:
                # Race with page close; benign.
                pass
        await self._context.route("**/*", _route_handler)

        # Single warmup nav so cf_clearance is freshened before the first
        # batch — without this, the first batch pays a 3-4s Cloudflare
        # challenge for ALL N pages simultaneously, which trips the
        # "burst-detect" heuristic and 0/N succeed.
        try:
            page = await self._context.new_page()
            await page.goto(_BASE_URL + "/", wait_until="commit", timeout=30000)
            # Brief settle so the cf_clearance cookie lands before we close.
            await page.wait_for_timeout(500)
            await page.close()
        except Exception as exc:
            _log(f"warmup nav warning: {exc}")

    # ── Batch capture ─────────────────────────────────────────────────────

    async def _capture_one(
        self, sema: asyncio.Semaphore, chapter_id: str, chapter_url: str
    ) -> Tuple[str, Optional[str], str]:
        """Returns (chapter_id, vrf_or_None, status). status is one of:
        'ok' | 'bounce' | 'error'. 'bounce' = redirected to homepage (CF
        signal); the caller may retry once."""
        async with sema:
            page = await self._context.new_page()
            captured_token: List[str] = []

            def on_request(req):
                u = req.url
                if "vrf=" in u and f"/chapter/{chapter_id}" in u:
                    try:
                        q = parse_qs(urlparse(u).query)
                        tok = (q.get("vrf") or [""])[0]
                        if tok and not captured_token:
                            captured_token.append(tok)
                    except Exception:
                        pass

            page.on("request", on_request)
            try:
                try:
                    await page.goto(chapter_url, wait_until="commit", timeout=45000)
                except Exception as exc:
                    _log(f"goto failed for {chapter_id}: {exc}")
                    return (chapter_id, None, "error")
                # Bounce-detect: did CF redirect us back to the homepage?
                final = page.url or ""
                parsed_final = urlparse(final)
                if parsed_final.path in ("", "/"):
                    return (chapter_id, None, "bounce")
                # Poll up to 5s for the VRF AJAX. The chapter page's
                # obfuscated bundle injects vrf= within ~500ms-2s typical.
                for _ in range(50):
                    if captured_token:
                        break
                    await asyncio.sleep(0.1)
                if captured_token:
                    return (chapter_id, captured_token[0], "ok")
                return (chapter_id, None, "error")
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _do_batch(
        self, chapters: List[Tuple[str, str]], parallel: int
    ) -> Dict[str, str]:
        await self._ensure_browser()
        sema = asyncio.Semaphore(max(1, int(parallel)))

        results = await asyncio.gather(
            *[self._capture_one(sema, cid, curl) for cid, curl in chapters]
        )

        captured: Dict[str, str] = {}
        bounced: List[Tuple[str, str]] = []
        for cid, tok, status in results:
            if status == "ok" and tok:
                captured[f"/ajax/read/chapter/{cid}"] = tok
            elif status == "bounce":
                bounced.append((cid, _url_for(cid, chapters)))

        # Bounce retry: drop cookies + retry the bounced ones, this time
        # serially (low conc) so CF doesn't lump them as another burst.
        if bounced:
            _log(
                f"async-batch: {len(bounced)} chapter(s) bounced to homepage; "
                "clearing cookies + retrying serially"
            )
            try:
                await self._context.clear_cookies()
            except Exception:
                pass
            # Single page warmup to mint fresh cf_clearance.
            try:
                page = await self._context.new_page()
                await page.goto(_BASE_URL + "/", wait_until="commit", timeout=30000)
                await page.wait_for_timeout(800)
                await page.close()
            except Exception:
                pass
            sema_seq = asyncio.Semaphore(1)
            retry_results = await asyncio.gather(
                *[self._capture_one(sema_seq, cid, curl) for cid, curl in bounced]
            )
            for cid, tok, status in retry_results:
                if status == "ok" and tok:
                    captured[f"/ajax/read/chapter/{cid}"] = tok
                else:
                    _log(f"async-batch: chapter {cid} retry also failed; foreground will recapture")

        return captured

    def submit_batch(
        self, chapters: List[Tuple[str, str]], parallel: int = 4
    ) -> Dict[str, str]:
        """Block until N chapters' VRFs are captured concurrently. Writes
        results into the sync-side _vrf_cache via populate_vrf_cache.
        Returns the captured dict (also for the caller's optional logging).

        Caller is the prefetch worker thread (sync). chapters is a list of
        (chapter_id, chapter_url). parallel = max in-flight pages. Caller
        passes the value sourced from --mangafire-vrf-parallel."""
        from sites.mangafire_vrf_simple import populate_vrf_cache

        if not chapters:
            return {}
        # Cap at the number of chapters we're actually capturing.
        parallel = max(1, min(int(parallel), len(chapters)))

        with self._init_lock:
            captured = self._run_coro(
                self._do_batch(chapters, parallel),
                timeout=120.0,
            )
        if captured:
            try:
                populate_vrf_cache(captured)
            except Exception as exc:
                _log(f"populate_vrf_cache failed: {exc}")
        return captured

    # ── Shutdown ──────────────────────────────────────────────────────────

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        async def _shutdown():
            try:
                if self._context is not None:
                    await self._context.close()
            except Exception:
                pass
            try:
                if self._browser is not None:
                    await self._browser.close()
            except Exception:
                pass
            try:
                if self._playwright_cm is not None:
                    await self._playwright_cm.__aexit__(None, None, None)
            except Exception:
                pass
        try:
            self._run_coro(_shutdown(), timeout=10.0)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass


def _url_for(cid: str, chapters: List[Tuple[str, str]]) -> str:
    """Helper: look up the URL for a given chapter_id in the batch list."""
    for c, u in chapters:
        if c == cid:
            return u
    return ""


# Module-level cleanup on process exit.
import atexit as _atexit
_INSTANCE: Optional[AsyncBatchVRFCapture] = None


def _cleanup_instance():
    global _INSTANCE
    if _INSTANCE is not None:
        try:
            _INSTANCE.close()
        except Exception:
            pass
        _INSTANCE = None


_atexit.register(_cleanup_instance)
