# Tsundoku — Handoff / Contexto do Projeto

App desktop em **Godot 4 (GDScript)** que serve de front-end para orquestrar CLI tools em **Python** que baixam mangás/light novels e os empacotam em EPUB/CBZ/PDF para leitura offline no e-reader XTEink X4.

O Godot é uma **camada fina de UI**. Toda a lógica pesada (scraping, download, merge) fica nos scripts Python, chamados como subprocessos. A comunicação de progresso é feita via **arquivo de status JSON** (polling), não por stdout.

---

## Decisões de arquitetura (já fechadas)

- **Linguagem Godot:** GDScript (não C#). Motivo: app utilitário, UI rápida de iterar, build leve sem dependência .NET.
- **Python NÃO é compilado para .exe.** Roda como script `.py` chamado pelo interpretador de uma **venv**. Motivo: empacotar Playwright + PyInstaller é um poço de problemas (Chromium não embarca no .exe).
- **A venv é chamada diretamente pelo caminho do interpretador**, sem "ativar". `.venv/Scripts/python.exe script.py ...` já resolve os pacotes da venv sozinho.
- **Comunicação de progresso via status file JSON** (não stdout). Mais robusto: sobrevive a buffering, o Godot só faz polling num Timer. Escrita atômica no Python (`.tmp` + `os.replace`) evita leitura parcial.

---

## Estrutura de pastas

```
tsundoku/                          # SEM caractere não-ASCII no path (ver "Lições" abaixo)
└─ content/
   └─ runtime/
      ├─ .venv/                    # venv com playwright, bs4, curl_cffi, tqdm, requests
      │  └─ Scripts/python.exe     # (no Linux: bin/python)
      ├─ centralnovel_dl.py        # downloader de light novels (PRONTO, v7)
      ├─ manga_dl.py               # downloader de mangá (AINDA falta integrar status-file + utf8)
      ├─ chapterbind.py            # merge de capítulos em EPUB/CBZ/PDF (AINDA falta integrar)
      ├─ downloads/                # saída dos downloads
      └─ temp/
         └─ status.json            # arquivo de progresso lido pelo Godot
```

---

## Contrato de status JSON (o "protocolo" entre Python e Godot)

Os scripts Python escrevem este arquivo; o Godot faz polling e reage. Todos os scripts devem seguir o mesmo contrato.

| `status`      | Quando                    | Campos-chave                                                        |
|---------------|---------------------------|--------------------------------------------------------------------|
| `starting`    | início                    | `url`                                                              |
| `preparing`   | catálogo carregado        | `title`, `total_chapters`, `progress: 0`                          |
| `downloading` | por capítulo              | `progress`, `current_chapter`, `total_chapters`, `chapter_num`     |
| `cooldown`    | espera forçada pelo site  | `wait_seconds`, `progress`, `current_chapter`, `attempt`           |
| `done`        | sucesso                   | `progress: 100`, `output_path`, `done`, `skipped`                 |
| `error`       | falha                     | `message`, `failed_chapters` (opcional)                           |
| `cancelled`   | Ctrl+C / interrupção      | `message`, `output_path`                                          |

Exemplo de payload:
```json
{ "status": "downloading", "timestamp": 1783181584.65, "progress": 42, "current_chapter": 12, "total_chapters": 40, "chapter_num": 12.0 }
```

O JSON usa `ensure_ascii=False` (acentos legíveis). Todo payload inclui `timestamp` (epoch float) automaticamente.

---

## Estado dos scripts Python

### centralnovel_dl.py — PRONTO (v7), validado ponta a ponta

Downloader de PDFs de light novels de `centralnovel.com` (plataforma Madara).

Pontos importantes da implementação:
- **O PDF é gerado por JS no browser** (html2pdf.js = html2canvas + jsPDF). Um GET simples só pega a casca HTML. Por isso usa **Playwright (Chromium headless)** para baixar o PDF de fato. A listagem de capítulos continua via curl_cffi (HTML estático).
- Tem rate limiter (token bucket), circuit breaker, backoff adaptativo, detecção de cooldown do site ("aguarde X segundos"), validação de assinatura `%PDF-`, filtro por `--chapters` e `--volumes`.
- Já tem `StatusReporter` (classe de escrita atômica) e o argumento `--status-file` integrado.
- Já tem o bloco de correção de encoding UTF-8 para Windows.

Uso validado:
```
.venv/Scripts/python.exe centralnovel_dl.py "<url_da_serie>" --chapters 1-40 --output downloads --status-file temp/status.json
```

### manga_dl.py — FALTA integrar

Precisa receber o **mesmo tratamento** que o centralnovel_dl.py:
1. Classe `StatusReporter` (escrita atômica de JSON, no-op se `--status-file` for None, nunca lança exceção pra cima).
2. Argumento `--status-file`.
3. Chamadas `reporter.write(...)` nos pontos do ciclo de vida seguindo o contrato acima.
4. Bloco de correção de encoding UTF-8 no topo (ver "Lições").

### chapterbind.py (v1.5.1) — FALTA integrar

Ferramenta de merge de capítulos em EPUB/CBZ/PDF único. Precisa do mesmo tratamento, MAS o significado do progresso é diferente:
- `progress` aqui é sobre **capítulos mesclados**, não baixados.
- Provavelmente tem uma **fase final de "escrevendo arquivo"** que vale reportar como um estado próprio (ex: `binding` ou `writing`) além do `done`.

---

## Snippet reutilizável: StatusReporter (Python)

Esta classe já está no centralnovel_dl.py e deve ser copiada para os outros dois scripts:

```python
import json, os, tempfile, time
from pathlib import Path
from typing import Optional

class StatusReporter:
    """Escreve progresso num JSON de forma atômica (.tmp + os.replace, atômico
    no Windows e Linux). No-op se status_file for None. NUNCA lança exceção pra
    cima — um erro de I/O no status não pode derrubar um download longo."""
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
                try: os.remove(tmp)
                except Exception: pass
```

## Snippet reutilizável: correção de encoding Windows (Python)

Colocar logo após os imports de stdlib, no topo de cada script:

```python
import sys
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
```

---

## Lições aprendidas no debug (IMPORTANTE — evitar reintroduzir)

1. **NUNCA usar caractere não-ASCII no path do projeto.** A pasta se chamava `tsundoku-積ん読` e o libcurl (via curl_cffi) falhava ao carregar o certificado CA (`curl: (77) error setting certificate verify locations`) porque não lida com kanji no path do `cacert.pem`. Renomear para `tsundoku` resolveu. O nome bonito com kanji fica na UI/título da janela, nunca no filesystem. Isso também afeta Playwright, PyInstaller e outras libs nativas.

2. **`PYTHONUTF8=1` é essencial no Windows.** Um erro de encoding do console (cp1252) mascarava a mensagem de erro real do SSL, e o retry wrapper fazia tudo parecer "falha de rede". Setar `PYTHONUTF8=1` descascou o erro real. O Godot deve setar isso antes de criar o processo.

3. **`sanitize_filename` troca `:` por `_`** — o título "Mushoku Tensei: Jobless Reincarnation" vira a pasta "Mushoku Tensei_ Jobless Reincarnation". Ao passar essa pasta pro chapterbind, ler o **nome real do diretório**, não reconstruir do título com `:`.

---

## Lado Godot — o que já existe e o que falta

### Já esboçado: script de UI de download (extends Control)

Já existe um script funcional para a tela de download que:
- Usa **`OS.create_process`** (NÃO `OS.execute`, que é bloqueante e congela a UI).
- Faz polling do `status.json` num `Timer` de 0.3s.
- Traduz cada estado do contrato num `match` que atualiza `ProgressBar` + `StatusLabel`.
- Limpa o `status.json` antigo antes de iniciar (senão lê "done" de rodada anterior).
- Seta `PYTHONUTF8=1` via `OS.set_environment`.
- Guarda contra crash silencioso (processo morre sem escrever status).
- Usa `ProjectSettings.globalize_path("res://content/runtime")` para converter caminhos (o Python roda fora do Godot e não entende `res://`).

Nodes que o `.tscn` precisa (com `unique_name_in_owner` ligado): `%UrlTextEdit` (TextEdit), `%DownloadButton` (Button), `%ProgressBar` (ProgressBar), `%StatusLabel` (Label).

### Layout / estrutura de cena (main menu)

Ordem de containers correta (de fora pra dentro), para o menu principal:
```
AspectRatioContainer (raiz)
└─ Panel (fundo)
   └─ MarginContainer (respiro da tela toda)
      └─ VBoxContainer (empilha título + menu verticalmente)  ← faltava isso; título e menu estavam como irmãos soltos e sobrepunham
         ├─ CenterContainer
         │  └─ AspectRatioContainer
         │     └─ Label ("Tsundoku 積ん読")
         └─ MenuCenterContainer (CenterContainer)
            └─ ButtonMarginContainer (MarginContainer, respiro interno dos botões)
               └─ VBoxContainer/GridContainer (botões: Manga, Novel, Fila, etc.)
```

### Design da UI (referência)

Estética "biblioteca/estante" (tsundoku = pilha de livros por ler). Sidebar com seções por tipo (Estante, Manga, Light novel, Fila, Biblioteca, Ajustes). Cada aba mapeia para um script Python. Barra de progresso fina com %, não spinner. Grid final "Prontos para o e-reader" com ícones diferenciados por formato (EPUB/CBZ/PDF).

---

## Roadmap sugerido (o que o Claude Code deve fazer)

1. **Integrar status-file + encoding UTF-8 no `manga_dl.py`** seguindo o padrão do centralnovel_dl.py e o contrato de status.
2. **Integrar no `chapterbind.py`**, adicionando o(s) estado(s) extra(s) da fase de escrita (`binding`/`writing`).
3. **Criar o `ProcessManager.gd` como autoload singleton** — centralizar a lógica de montar args, criar processo, fazer polling e emitir sinais Godot (`progress_updated`, `job_finished`, `job_failed`). As telas ficam "burras" e só escutam os sinais. Hoje a lógica está dentro da tela de download; extrair para o singleton.
4. **Adicionar UI de seleção**: campo de capítulos (`--chapters`), `OptionButton` manga/novel, campo de volumes opcional.
5. **Fila de jobs (JobQueue)** — mesmo que simples (lista + um por vez), evita saturar conexão/CPU. Usar um **status file por job** (`temp/status_<job_id>.json`) em vez do fixo, senão downloads paralelos sobrescrevem o mesmo arquivo.
6. **Pipeline encadeado**: após download concluir (`done`), oferecer/disparar automaticamente o chapterbind naquela pasta de saída.

---

## Setup da venv (uma vez, referência)

```
cd content/runtime
python -m venv .venv
.venv/Scripts/python.exe -m pip install requests beautifulsoup4 playwright curl_cffi tqdm
.venv/Scripts/playwright.exe install chromium
```
No Linux (CachyOS), trocar `Scripts/` por `bin/` e rodar também `playwright install-deps chromium`.

Se o app for rodar principalmente no CachyOS/Arch (daily driver do dev), considerar rodar tudo nativo em Linux — elimina os problemas de encoding e path do Windows de uma vez.
