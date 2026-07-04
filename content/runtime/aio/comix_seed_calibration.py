"""Standalone comix seed calibrator.

Drives ComixSiteHandler end-to-end on a list of comix.to URLs and emits the
aggregate T1+T2 quality score that the orchestrator's _probe_chapter_aggregate
would have computed — minus the orchestrator's 240 s probe deadline and the
60 s bridge timeout that together prevent the real probe from ever finishing
on comix.

Why this tool exists
====================
sites/comix.py canvas-captures every page of a chapter to defeat the site's
client-side image scrambling. A single chapter takes 30–180 s through the
single-worker Patchright bridge. The probe phase needs 8 chapters/source ×
6 parallel sources, all queueing on that one bridge worker — the orchestrator
hits its 240 s wall-clock deadline long before comix produces a single score.
Worse, when it DID complete, the synthetic ``comix-page://`` URLs the canvas
capture returns aren't HTTP-fetchable by ``_fetch_probe_item_bytes`` so every
chapter scored 0.0 anyway.

This tool runs the probe sequentially with no outer wall-clock, resolves the
synthetic URLs out of sites/image_cache (where comix's response listener
stashes the decoded bytes), and feeds them through the same _score_image_blob
path the probe would have used — with --enable-ml-rating forced on so T2 NIQE
+ CLIP-IQA+ contribute to the composite.

Output
======
Per URL: JSON with aggregate_score + per-chapter scores + content classification.
At the end: arithmetic mean of all aggregate scores — the calibrated seed
that should land in sites/quality_seed.json under "comix".

Usage
=====
    python comix_seed_calibration.py URL [URL ...]

Cross-file
==========
- sites/comix.py:_COMIX_BROWSER_BRIDGE — bridge facade used here for the
  bytes path. _COMIX_DEFAULT_TIMEOUT_S is the per-call cap; bumped to 900 s
  below so 130+ page chapters don't trip it.
- sites/base.py:_pick_representative_chapters / _pick_random_middle_page_index
  — same sampler the real probe uses.
- sites/search_orchestrator.py:_score_image_blob — full T1+T2 pipeline.
  Gated on _ML_RATING_ENABLED which we flip True via the env var BEFORE
  importing the module.
"""
from __future__ import annotations

# Must precede any sites.* import so the module-level _ML_RATING_ENABLED
# read picks up the gate. set_ml_rating_enabled also works post-import but
# the env-var path is one less moving part.
import os
os.environ.setdefault("AIO_ENABLE_ML_RATING", "1")

import json
import statistics
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import cloudscraper

import sites.comix as comix_mod
from sites import image_cache
from sites.base import SearchHit
from sites.comix import ComixSiteHandler
from sites.search_orchestrator import (
    _classify_series_content,
    _score_image_blob,
    is_ml_rating_enabled,
    set_ml_rating_enabled,
    warmup_t2_models,
)

# Bump the bridge's default per-call timeout. The real probe relies on the
# per-method _timeout_s plumbing (fetch_chapter_images_via_dom defaults to
# 300+30 s) so _COMIX_DEFAULT_TIMEOUT_S only matters for calls that don't
# pass an explicit timeout — but bumping it costs nothing and guards
# against future bridge methods that forget.
comix_mod._COMIX_DEFAULT_TIMEOUT_S = 900.0


def _make_request(url: str, scraper):
    """Minimal make_request shim — single GET, 30 s timeout, no retry.

    Comix's _cf_aware_request wraps this with zendriver CF resilience on
    403/503, so we don't need our own retry loop. Search-time
    _search_make_request_factory does retries; for one-off calibration we
    don't bother.
    """
    return scraper.get(url, timeout=30)


def _resolve_to_bytes(
    item: Any, scraper,
) -> Optional[Tuple[bytes, str]]:
    """Resolve a get_chapter_images return item to (bytes, content_type).

    Item shapes:
      - str starting with ``comix-page://`` — synthetic key for a canvas-
        captured page. Bytes live in sites/image_cache under that key.
      - other str — plain CDN URL for a non-scrambled <img>. Fetch via
        the supplied cloudscraper instance (CF cookies already loaded).

    Returns None on miss / fetch failure / undersized body.
    """
    if not isinstance(item, str) or not item:
        return None
    cached = image_cache.get_cached_image(item)
    if cached is not None:
        return cached
    if item.startswith("comix-page://"):
        # Synthetic URL that should have been in cache but wasn't —
        # likely TTL-evicted between scrape and read. We don't retry
        # because re-scraping the chapter would just re-populate the
        # same key on a 600 s budget that's already gone.
        return None
    try:
        resp = scraper.get(item, timeout=30)
        if resp.status_code >= 400:
            return None
        body = resp.content
        if not body or len(body) < 256:
            return None
        ct = resp.headers.get("content-type", "")
        return (body, ct)
    except Exception:
        return None


def calibrate_one(url: str, n_chapters: int = 8) -> Dict[str, Any]:
    """Run the full _probe_chapter_aggregate-equivalent pipeline on one URL.

    Returns a dict with aggregate_score + per-chapter detail. Mirrors the
    shape that the orchestrator's per-source cache stores so the numbers
    can be compared directly against other sites' cached scores.
    """
    print(f"\n{'=' * 70}", flush=True)
    print(f"[*] calibrating: {url}", flush=True)
    print(f"{'=' * 70}", flush=True)
    handler = ComixSiteHandler()
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "mobile": False}
    )
    handler.configure_session(scraper, args=None)

    t0 = time.monotonic()

    # Step 1: fetch_comic_context (1 HTTP, CF-resilient via _cf_aware_request)
    print("[*] fetch_comic_context...", flush=True)
    t_ctx = time.monotonic()
    try:
        ctx = handler.fetch_comic_context(url, scraper, _make_request)
    except Exception as exc:
        print(f"[!] fetch_comic_context failed: {type(exc).__name__}: {exc}", flush=True)
        return {"url": url, "error": f"fetch_comic_context: {exc}"}
    print(
        f"    title={ctx.title!r} ({time.monotonic() - t_ctx:.1f}s)", flush=True,
    )

    # Step 2: get_chapters (encrypted API → bridge DOM scrape)
    print("[*] get_chapters...", flush=True)
    t_ch = time.monotonic()
    try:
        chapters = handler.get_chapters(ctx, scraper, "en", _make_request)
    except Exception as exc:
        print(f"[!] get_chapters failed: {type(exc).__name__}: {exc}", flush=True)
        return {"url": url, "error": f"get_chapters: {exc}"}
    n_chap_total = len(chapters)
    print(
        f"    {n_chap_total} chapter(s) ({time.monotonic() - t_ch:.1f}s)",
        flush=True,
    )
    if not chapters:
        return {"url": url, "title": ctx.title, "error": "no chapters"}

    # Step 3: pick N representative chapters via the same sampler the
    # orchestrator uses.
    chapter_picks = ComixSiteHandler._pick_representative_chapters(
        chapters, n=n_chapters,
    )
    print(
        f"[*] sampling {len(chapter_picks)} chapter(s) at indices "
        f"{[i for i, _ in chapter_picks]}",
        flush=True,
    )

    # Step 4: per-chapter scrape + score (interleaved so image_cache TTL
    # doesn't expire between scrape and read on slow runs).
    per_chapter_scores: List[float] = []
    per_chapter_metas: List[Dict] = []
    per_chapter_blobs: List[Optional[bytes]] = []
    per_chapter_records: List[Dict[str, Any]] = []

    for abs_idx, chapter in chapter_picks:
        t_chap = time.monotonic()
        chap_label = chapter.get("chap") or "?"
        chap_url = chapter.get("url") or "?"
        print(
            f"  • idx={abs_idx} chap={chap_label!r} "
            f"url={chap_url}",
            flush=True,
        )

        try:
            image_items = handler.get_chapter_images(
                chapter, scraper, _make_request,
            )
        except Exception as exc:
            print(
                f"      ! get_chapter_images raised "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            per_chapter_scores.append(0.0)
            per_chapter_blobs.append(None)
            per_chapter_records.append({
                "idx": abs_idx, "score": 0.0,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue

        n_imgs = len(image_items) if image_items else 0
        dt_scrape = time.monotonic() - t_chap
        print(f"      → {n_imgs} image(s) ({dt_scrape:.1f}s)", flush=True)

        if not image_items:
            per_chapter_scores.append(0.0)
            per_chapter_blobs.append(None)
            per_chapter_records.append({
                "idx": abs_idx, "score": 0.0, "reason": "no_images",
            })
            continue

        page_idx = ComixSiteHandler._pick_random_middle_page_index(
            len(image_items), url, abs_idx, chapter=chapter,
        )
        if page_idx is None:
            per_chapter_scores.append(0.0)
            per_chapter_blobs.append(None)
            per_chapter_records.append({
                "idx": abs_idx, "score": 0.0, "reason": "no_page_picked",
            })
            continue

        resolved = _resolve_to_bytes(image_items[page_idx], scraper)
        if resolved is None:
            per_chapter_scores.append(0.0)
            per_chapter_blobs.append(None)
            per_chapter_records.append({
                "idx": abs_idx, "score": 0.0,
                "reason": "no_bytes",
                "picked_item": image_items[page_idx],
            })
            print(
                f"      ! resolved no bytes for "
                f"{image_items[page_idx]!r}",
                flush=True,
            )
            continue
        blob, ct = resolved

        # First pass: score with content_type="unknown" so we can
        # classify the series across all 8 samples below.
        scored = _score_image_blob(blob)
        if scored is None:
            per_chapter_scores.append(0.0)
            per_chapter_blobs.append(None)
            per_chapter_records.append({
                "idx": abs_idx, "score": 0.0, "reason": "score_failed",
            })
            continue
        score, metadata = scored
        per_chapter_scores.append(score)
        per_chapter_metas.append(metadata)
        per_chapter_blobs.append(blob)
        per_chapter_records.append({
            "idx": abs_idx,
            "score": score,
            "width": metadata.get("width"),
            "height": metadata.get("height"),
            "is_grayscale": metadata.get("is_grayscale"),
            "format": metadata.get("format"),
            "t1_score": metadata.get("t1_score"),
            "t2_available": metadata.get("t2_available"),
            "t2_score": metadata.get("t2_score"),
            "niqe_score": metadata.get("niqe_score"),
            "clip_iqa_score": metadata.get("clip_iqa_score"),
        })
        print(
            f"      ✓ score={score:.4f} "
            f"t1={metadata.get('t1_score')!s:.6} "
            f"t2={metadata.get('t2_score')!s:.6} "
            f"({metadata.get('width')}×{metadata.get('height')} "
            f"{metadata.get('format')})",
            flush=True,
        )

    # Step 5: classify content_type from per-page metadata and re-score
    # successful blobs if the classification produced a non-trivial
    # content_type. Mirrors base.py:_probe_chapter_aggregate.
    series_content_type = "unknown"
    if per_chapter_metas:
        try:
            feature_view = [
                {
                    "width": m.get("width", 0),
                    "height": m.get("height", 0),
                    "aspect": (
                        m.get("width", 0) / m["height"]
                        if m.get("height") else 1.0
                    ),
                    "is_grayscale_page": bool(m.get("is_grayscale", False)),
                    "chroma_var": float(m.get("chroma_var", 0.0)),
                }
                for m in per_chapter_metas
            ]
            series_content_type = _classify_series_content(feature_view)
        except Exception as exc:
            print(
                f"[!] classify_series_content failed: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    print(f"[*] series content_type = {series_content_type!r}", flush=True)

    if series_content_type not in ("unknown", "bw_manga") and per_chapter_blobs:
        print(
            f"[*] re-scoring with content_type={series_content_type!r}...",
            flush=True,
        )
        new_scores = list(per_chapter_scores)
        rescore_idx = 0
        for i, old_score in enumerate(per_chapter_scores):
            if old_score <= 0.0:
                continue
            blob = per_chapter_blobs[i]
            if blob is None:
                rescore_idx += 1
                continue
            r = _score_image_blob(blob, content_type=series_content_type)
            if r is None:
                rescore_idx += 1
                continue
            new_score, _new_meta = r
            new_scores[i] = new_score
            rescore_idx += 1
        per_chapter_scores = new_scores

    # Step 6: aggregate — median when all succeeded, mean otherwise.
    successes = [s for s in per_chapter_scores if s > 0.0]
    if not successes:
        aggregate = 0.0
    elif len(successes) == len(per_chapter_scores):
        aggregate = statistics.median(per_chapter_scores)
    else:
        aggregate = sum(per_chapter_scores) / len(per_chapter_scores)

    result = {
        "url": url,
        "title": ctx.title,
        "content_type": series_content_type,
        "n_chapters_total": n_chap_total,
        "n_chapters_probed": len(chapter_picks),
        "n_chapters_succeeded": len(successes),
        "aggregate_score": round(float(aggregate), 4),
        "per_chapter": per_chapter_records,
        "per_chapter_scores": [round(float(s), 4) for s in per_chapter_scores],
        "elapsed_s": round(time.monotonic() - t0, 1),
    }
    print(
        f"\n[✓] {url}\n    aggregate_score = {aggregate:.4f} "
        f"(n={len(successes)}/{len(per_chapter_scores)} successful, "
        f"content_type={series_content_type!r}, "
        f"{result['elapsed_s']:.1f}s)",
        flush=True,
    )
    return result


def _shutdown_bridge() -> None:
    """Best-effort: tell the comix bridge to close its Patchright session.
    Daemon thread, so the interpreter exits regardless."""
    try:
        comix_mod._COMIX_REQUEST_QUEUE.put_nowait(
            comix_mod._COMIX_SHUTDOWN_SENTINEL
        )
    except Exception:
        pass


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "usage: python comix_seed_calibration.py URL [URL ...]",
            file=sys.stderr,
        )
        return 1
    urls = sys.argv[1:]

    print("[*] enabling ML rating + warming T2 models...", flush=True)
    set_ml_rating_enabled(True)
    t_warm = time.monotonic()
    warmup_t2_models(background=False)
    print(
        f"[*] T2 ready in {time.monotonic() - t_warm:.1f}s. "
        f"ml_rating_enabled={is_ml_rating_enabled()}",
        flush=True,
    )

    results: List[Dict[str, Any]] = []
    for url in urls:
        try:
            r = calibrate_one(url)
        except Exception as exc:
            print(
                f"[!] {url} top-level exception: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            traceback.print_exc()
            r = {"url": url, "error": f"{type(exc).__name__}: {exc}"}
        results.append(r)

    scores = [
        r["aggregate_score"]
        for r in results
        if isinstance(r.get("aggregate_score"), (int, float))
    ]

    summary = {
        "results": results,
        "average_score": (
            round(sum(scores) / len(scores), 4) if scores else None
        ),
        "n_scored": len(scores),
        "n_total": len(results),
    }
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(json.dumps(summary, indent=2, default=str))
    if scores:
        print(
            f"\n→ Suggested comix seed (mean of {len(scores)} title(s)): "
            f"{summary['average_score']}",
            flush=True,
        )
    _shutdown_bridge()
    return 0


if __name__ == "__main__":
    sys.exit(main())
