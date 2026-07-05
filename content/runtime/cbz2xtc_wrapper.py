#!/usr/bin/env python3
"""
cbz2xtc_wrapper.py — ponte entre o Tsundoku (Godot) e o cbz2xtc.py.

O cbz2xtc converte uma PASTA de CBZs no formato XTC/XTCH do e-reader XTEink X4,
gerando os arquivos em <pasta>/xtc_output/. Ele não escreve status-file e imprime
com caracteres Unicode (✓/✗) que quebram no cp1252 do Windows. Este wrapper roda
o cbz2xtc como subprocesso (com PYTHONUTF8=1) e traduz o resultado pro mesmo
contrato JSON dos outros, pro Godot fazer polling igual.

Roda na venv/embeddable enxuta (numpy + pillow). numba é opcional (acelera muito
via JIT; sem ele o cbz2xtc usa um fallback lento).

Uso (o Godot monta isto):
  python cbz2xtc_wrapper.py --status-file <temp/status_<id>.json> <PASTA> [opções cbz2xtc]
Tudo depois da pasta é repassado ao cbz2xtc (--2bit, --dither, --compress, ...).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


class StatusReporter:
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


CBZ2XTC = Path(__file__).resolve().parent / "cbz2xtc.py"


def main() -> int:
    p = argparse.ArgumentParser(description="Wrapper de status para o cbz2xtc")
    p.add_argument("--status-file", default=None)
    p.add_argument("folder", help="Pasta com os CBZs a converter")
    args, rest = p.parse_known_args()

    reporter = StatusReporter(args.status_file)
    reporter.write("starting")

    if not CBZ2XTC.exists():
        reporter.write("error", message=f"cbz2xtc.py não encontrado em {CBZ2XTC}")
        return 1

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"          # sem isto o ✓/✗ do cbz2xtc quebra no cp1252
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [sys.executable, str(CBZ2XTC), args.folder, *rest]

    # "preparing" só pra a UI sair do "iniciando"; a conversão é uma operação
    # longa e única (sem progresso por página confiável no stdout).
    reporter.write("preparing", title=Path(args.folder).name, total_chapters=0, progress=0)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
        )
    except Exception as e:
        reporter.write("error", message=f"falha ao iniciar cbz2xtc: {e}")
        return 1

    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()

    rc = proc.wait()
    out_dir = os.path.join(args.folder, "xtc_output")
    if rc == 0:
        reporter.write("done", progress=100, output_path=out_dir)
    else:
        reporter.write("error", message=f"cbz2xtc saiu com código {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
