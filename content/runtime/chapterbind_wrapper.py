#!/usr/bin/env python3
"""
chapterbind_wrapper.py — ponte entre o Tsundoku (Godot) e o pacote chapterbind.

O chapterbind junta uma pasta de capítulos (PDF/EPUB/CBZ) num arquivo único (e
também separa em volumes). É um pacote Python, não um script solto, e não escreve
status-file. Este wrapper roda o chapterbind IN-PROCESS (importando cli.run) e
traduz o resultado pro mesmo contrato JSON dos downloaders, pro Godot fazer
polling igual faz com o resto.

Roda na venv/embeddable enxuta do Tsundoku (só precisa de pypdf + pillow, já no
requirements.txt). Adiciona o próprio diretório (runtime/) ao sys.path pra o
pacote chapterbind ser importável — o embeddable não adiciona o dir sozinho.

Uso (o Godot monta isto):
  python chapterbind_wrapper.py --status-file <temp/status_<id>.json> \
      <PASTA_DE_CAPITULOS> -f pdf -o <SAIDA> -t "<Titulo>"
Tudo depois de --status-file é repassado ao chapterbind sem alteração.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# runtime/ no sys.path pra `import chapterbind` funcionar (o embeddable não
# adiciona o diretório do script sozinho).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


def _output_from_args(rest: list[str]) -> str:
    """Extrai o -o/--output dos args do chapterbind, só pra reportar no status."""
    for flag in ("-o", "--output"):
        if flag in rest:
            i = rest.index(flag)
            if i + 1 < len(rest):
                return rest[i + 1]
    return ""


def main() -> int:
    p = argparse.ArgumentParser(description="Wrapper de status para o chapterbind")
    p.add_argument("--status-file", default=None, help="JSON de progresso pro Godot")
    args, rest = p.parse_known_args()

    reporter = StatusReporter(args.status_file)
    reporter.write("starting")

    try:
        from chapterbind.cli import run
    except Exception as e:
        reporter.write("error", message=f"não consegui importar chapterbind: {e}")
        print(f"[wrapper] import falhou: {e}", file=sys.stderr)
        return 1

    # "binding" = juntando/escrevendo. É uma operação única (sem progresso por
    # capítulo granular), então reportamos o estado e o resultado.
    reporter.write("binding")

    try:
        rc = run(rest)
    except SystemExit as e:  # argparse do chapterbind pode chamar sys.exit
        rc = e.code if isinstance(e.code, int) else 1
    except Exception as e:
        reporter.write("error", message=str(e))
        print(f"[wrapper] chapterbind falhou: {e}", file=sys.stderr)
        return 1

    if rc == 0:
        reporter.write("done", progress=100, output_path=_output_from_args(rest))
    else:
        reporter.write("error", message=f"chapterbind saiu com código {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
