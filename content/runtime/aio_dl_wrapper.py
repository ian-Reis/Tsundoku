#!/usr/bin/env python3
"""
aio_dl_wrapper.py — ponte entre o Tsundoku (Godot) e o AIO-Webtoon-Downloader.

O `aio/aio-dl.py` é uma ferramenta de terceiros (baixa de mangafire.to e vários
outros sites que o mangalivre_dl não cobre). Ela NÃO escreve status-file — só
imprime progresso no stdout. Este wrapper roda o aio-dl.py, lê o stdout dele e
traduz para o mesmo contrato JSON dos outros downloaders do Tsundoku, para o
Godot fazer polling igual faz com centralnovel_dl e mangalivre_dl.

Este wrapper usa só a stdlib — pode rodar em qualquer Python. Mas o aio-dl.py
tem dependências pesadas (patchright, curl_cffi, pyvips, fastapi, numpy, ...),
então o interpretador que roda o FILHO precisa ser o de um venv com essas libs.
Passe-o em --aio-python (padrão: o mesmo Python que roda este wrapper).

Uso (o Godot monta isto):
  python aio_dl_wrapper.py "<url>" --chapters 1-10 --output <dir> \
      --status-file <temp/status_<id>.json> --aio-python <python_do_aio>

Marcadores de stdout do aio-dl.py que a gente escuta:
  "  Selected N chapters."      -> total (status "preparing")
  "Chapter N (...)"             -> começou um capítulo (status "downloading")
  "CBZ saved → ..."            -> um capítulo concluído
  "Done."                       -> sucesso  (status "done")
  "ABORTED. ..."               -> falha    (status "error")
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# Encoding do console no Windows (a saga do cp1252): sem isso, títulos/emojis do
# aio-dl.py podem quebrar o pipe de leitura.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


class StatusReporter:
    """Escreve o progresso num JSON de forma atômica (.tmp + os.replace). No-op
    se status_file for None. Nunca lança exceção pra cima."""

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
        except Exception:
            if tmp is not None and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass


AIO_DIR = Path(__file__).resolve().parent / "aio"
AIO_SCRIPT = AIO_DIR / "aio-dl.py"

RE_TOTAL = re.compile(r"Selected\s+(\d+)\s+chapters")
RE_CHAPTER = re.compile(r"^Chapter\s+(\d+(?:\.\d+)?)\b")
RE_SAVED = re.compile(r"saved\s*(?:→|->)")   # "CBZ saved → ...", "PDF Chapter saved → ..."
RE_DONE = re.compile(r"^Done\.")
RE_ABORT = re.compile(r"^ABORTED")


def _title_from_url(url: str) -> str:
    """Nome provisório legível a partir da URL (o aio-dl não imprime um título
    limpo de forma confiável)."""
    try:
        slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    except Exception:
        slug = ""
    slug = re.sub(r"\.[a-z0-9]{4,8}$", "", slug)     # tira sufixo tipo ".3rk0y"
    slug = slug.replace("-", " ").replace("_", " ").strip()
    return slug.title() if slug else url


def main() -> int:
    p = argparse.ArgumentParser(description="Wrapper de status para o AIO-Webtoon-Downloader")
    p.add_argument("url", help="URL da série")
    p.add_argument("--chapters", default="all", help="Filtro de capítulos (ex: all, 1-10, 1,3,5)")
    p.add_argument("--output", default="./downloads", help="Pasta de saída")
    p.add_argument("--status-file", default=None, help="JSON de progresso pro Godot")
    p.add_argument("--language", default="pt-br", help="Idioma preferido (padrão: pt-br)")
    p.add_argument("--format", default="cbz", help="Formato de saída (padrão: cbz)")
    p.add_argument("--skip-failed", action="store_true", help="Passa --skip-failed pro aio-dl")
    p.add_argument("--aio-python", default=sys.executable,
                   help="Interpretador com as deps do aio-dl (padrão: este mesmo Python)")
    args = p.parse_args()

    reporter = StatusReporter(args.status_file)

    if not AIO_SCRIPT.exists():
        reporter.write("error", message=f"aio-dl.py não encontrado em {AIO_SCRIPT}")
        print(f"[wrapper] aio-dl.py não encontrado: {AIO_SCRIPT}", file=sys.stderr)
        return 1

    reporter.write("starting", url=args.url)

    cmd = [
        args.aio_python, str(AIO_SCRIPT), args.url,
        "--language", args.language,
        "--format", args.format,
        "-o", args.output,
        "--chapters", args.chapters,
    ]
    if args.skip_failed:
        cmd.append("--skip-failed")

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"   # sem isso o stdout do filho vem em blocos e o progresso trava

    title = _title_from_url(args.url)
    total = 0
    started = 0   # capítulos iniciados (linhas "Chapter N")
    done = 0      # capítulos salvos
    aborted = False

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(AIO_DIR),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=env,
        )
    except Exception as e:
        reporter.write("error", message=f"falha ao iniciar aio-dl: {e}")
        print(f"[wrapper] falha ao iniciar aio-dl: {e}", file=sys.stderr)
        return 1

    def _terminate(*_):
        # Se este wrapper for interrompido (Ctrl+C / SIGTERM), tenta derrubar o
        # filho junto. Em kill "duro" (TerminateProcess no Windows) isto não
        # roda — ver limitação documentada no README.
        try:
            proc.terminate()
        except Exception:
            pass

    signal.signal(signal.SIGINT, _terminate)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _terminate)

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)   # repassa o log do aio-dl pro terminal
            sys.stdout.flush()
            stripped = line.strip()

            m = RE_TOTAL.search(line)
            if m:
                total = int(m.group(1))
                reporter.write("preparing", title=title, total_chapters=total, progress=0)
                continue

            m = RE_CHAPTER.match(stripped)
            if m:
                started += 1
                progress = round((started - 1) / total * 100) if total else 0
                reporter.write(
                    "downloading",
                    progress=progress,
                    current_chapter=started,
                    total_chapters=total,
                    chapter_num=float(m.group(1)),
                    done=done,
                )
                continue

            if RE_SAVED.search(line):
                done += 1
                progress = round(done / total * 100) if total else 0
                reporter.write(
                    "downloading",
                    progress=progress,
                    current_chapter=max(started, done),
                    total_chapters=total,
                    done=done,
                )
                continue

            if RE_ABORT.match(stripped):
                aborted = True
    except KeyboardInterrupt:
        _terminate()

    rc = proc.wait()

    if aborted or rc != 0:
        reporter.write(
            "error",
            message="download abortado" if aborted else f"aio-dl saiu com código {rc}",
            progress=round(done / total * 100) if total else 0,
            output_path=args.output,
            done=done,
        )
        return rc if rc != 0 else 2

    reporter.write("done", progress=100, output_path=args.output, done=done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
