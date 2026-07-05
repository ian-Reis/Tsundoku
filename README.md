# Tsundoku 積ん読

> *Tsundoku* (積ん読) — o hábito de acumular livros para ler "depois".

App desktop que baixa **mangás** e **light novels** da web e os empacota em
**EPUB / CBZ / PDF** para leitura offline no e-reader. Uma UI feita em **Godot 4**
orquestra ferramentas de linha de comando em **Python** que fazem o trabalho
pesado (scraping, download e merge dos capítulos).

---
<img width="1296" height="861" alt="Screenshot 2026-07-04 184013" src="https://github.com/user-attachments/assets/8843a495-7d8b-48eb-850c-fa875c0de8d9" />

## ✨ Recursos

- 📥 **Fila de downloads** — enfileire várias séries; rodam uma de cada vez para
  não saturar a conexão nem o site de origem.
- 📊 **Progresso em tempo real** — cada item da fila mostra título e barra de
  progresso, atualizados capítulo a capítulo.
- 🎯 **Seleção de volumes e capítulos** — baixe a série inteira ou só um intervalo
  (`1-5`, `1,3,5`, `5-`, `-10`).
- ✖️ **Cancelamento por item** — cancele um download em andamento ou remova um
  item que ainda está esperando na fila.
- 📦 **Empacotamento** em EPUB, CBZ ou PDF prontos para o e-reader.
- 🌐 **Multi-idioma** — interface em PT-BR / English (troca em runtime, preferência
  salva) e seletor separado para o idioma do conteúdo baixado.

---

## 🏗️ Arquitetura

O Godot é apenas uma **camada fina de UI**. Toda a lógica de scraping, download e
merge fica nos scripts Python, chamados como **subprocessos** (`OS.create_process`,
que não bloqueia a interface).

A comunicação de progresso é feita por **arquivo de status JSON** (não por stdout):
o Python escreve o progresso de forma atômica (`.tmp` + `os.replace`) e o Godot faz
**polling** desse arquivo num `Timer`. É mais robusto — sobrevive a buffering e a
leituras parciais.

```
┌─────────────────┐   create_process    ┌────────────────────────┐
│   Godot (UI)    │ ──────────────────► │  Python (venv)         │
│                 │                      │  centralnovel_dlv7.py  │
│  fila de jobs   │ ◄─── status.json ─── │  mangalivre_dlv4.py    │
│  polling 0.3s   │     (polling)        │  aio_dl_wrapper.py ──┐ │
└─────────────────┘                      └──────────────────────┼─┘
																 │ stdout
														  ┌──────▼───────┐
														  │ aio/aio-dl.py│
														  │ (venv própria)│
														  └──────────────┘
```

### Contrato de status (protocolo Python ↔ Godot)

Todos os scripts escrevem o mesmo formato de JSON:

| `status`      | Quando                   | Campos-chave                                           |
|---------------|--------------------------|--------------------------------------------------------|
| `starting`    | início                   | `url`                                                  |
| `preparing`   | catálogo carregado       | `title`, `total_chapters`, `progress`                  |
| `downloading` | por capítulo             | `progress`, `current_chapter`, `total_chapters`        |
| `cooldown`    | espera imposta pelo site | `wait_seconds`, `progress`                             |
| `done`        | sucesso                  | `progress: 100`, `output_path`, `done`                 |
| `error`       | falha                    | `message`, `failed_chapters` (opcional)                |
| `cancelled`   | interrupção              | `message`, `output_path`                               |

---

## 📁 Estrutura

```
tsundoku/                        # sem caracteres não-ASCII no caminho (ver Notas)
├─ content/
│  ├─ autoload/                  # singletons globais (registrados no project.godot)
│  │  ├─ paths.gd                # resolve python/venv/dirs por-OS (Scripts vs bin)
│  │  └─ process_manager.gd      # motor: fila + spawn + polling → sinais (sem UI)
│  ├─ core/                      # classes de domínio (class_name, sem UI)
│  │  ├─ job.gd                  # modelo do download; monta os args
│  │  ├─ source_registry.gd      # fontes como DADOS (Sources.DEFS)
│  │  └─ status_contract.gd      # estados/campos do status.json + leitura
│  ├─ i18n/i18n.gd               # textos PT-BR/EN (autoload I18n)
│  ├─ execute/                   # tela de download (VIEW fina)
│  │  ├─ execute.gd / .tscn      # monta jobs e escuta os sinais do ProcessManager
│  │  └─ task.gd / .tscn         # card de um item da fila
│  ├─ fonts/  ·  icons/
│  └─ runtime/                   # o "back-end" Python
│     ├─ .venv/                  # venv: playwright, bs4, curl_cffi, requests…
│     ├─ centralnovel_dlv7.py    # downloader de light novels
│     ├─ mangalivre_dlv4.py      # downloader de mangá (mangalivre.to e afins)
│     ├─ aio_dl_wrapper.py       # ponte: traduz o stdout do AIO → status.json
│     ├─ aio/                    # AIO-Webtoon-Downloader (3rd-party, venv própria)
│     ├─ chapterbind.py          # merge → EPUB/CBZ/PDF (em integração)
│     ├─ downloads/              # saída
│     └─ temp/status_*.json      # arquivos de progresso lidos pelo Godot
└─ project.godot
```

---

## 🚀 Setup

### Pré-requisitos
- [Godot 4.7+](https://godotengine.org/) (GDScript, sem .NET)
- Python 3.10+

### Ambiente Python (uma vez)

```bash
cd content/runtime
python -m venv .venv
.venv/Scripts/python.exe -m pip install requests beautifulsoup4 playwright curl_cffi tqdm
.venv/Scripts/playwright.exe install chromium
```

No Linux, troque `Scripts/` por `bin/` e rode também `playwright install-deps chromium`.

### Fonte "Mangá (AIO)" — venv separada

O **AIO-Webtoon-Downloader** (`content/runtime/aio/`) é uma ferramenta de
terceiros com dependências pesadas (patchright, curl_cffi, pyvips, fastapi,
numpy…), várias delas trabalhosas no Windows. Por isso ele roda numa **venv
própria**, isolada da venv enxuta do Tsundoku:

```bash
cd content/runtime/aio
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

O Godot chama o `aio_dl_wrapper.py` (que só usa stdlib) pela venv do Tsundoku, e
o wrapper spawna o `aio-dl.py` pela venv do AIO (`--aio-python`). O wrapper lê o
stdout do AIO e o traduz para o mesmo `status.json` dos outros downloaders.

> ⚠️ **Cancelar um job do AIO** mata o wrapper, mas em kill "duro" no Windows o
> `aio-dl.py` filho pode continuar rodando órfão. Fix planejado: derrubar a
> árvore de processos (`taskkill /T` / process group). Dado o peso das deps, o
> AIO tende a rodar melhor no Linux (onde já funciona nativamente).

### Rodar

Abra o projeto no Godot e execute a cena principal. Para testar um script isolado:

```bash
.venv/Scripts/python.exe centralnovel_dlv7.py "<url>" \
	--chapters 1-40 --output downloads --status-file temp/status.json
```

---

## 🧩 Estado dos componentes

| Componente             | Estado                                                    |
|------------------------|-----------------------------------------------------------|
| `centralnovel_dlv7.py` | ✅ Pronto — validado ponta a ponta                        |
| `mangalivre_dlv4.py`   | ✅ Integrado — status-file + encoding + fila               |
| `mangadex_dl.py`       | ✅ Integrado — usa a venv enxuta (só requests)             |
| `aio_dl_wrapper.py`    | ✅ Wrapper pronto — falta criar a venv do AIO e validar    |
| Fila / UI (Godot)      | ✅ Fila (4 fontes), progresso, volumes/caps e cancelamento |
| `chapterbind.py`       | 🚧 Falta integrar (estado extra de "escrevendo arquivo")  |
| Pipeline encadeado     | 📋 Planejado — disparar o merge após o download            |

---

## 📝 Notas técnicas

- **Nada de caracteres não-ASCII no caminho do projeto.** Libs nativas (curl_cffi,
  Playwright) falham ao carregar certificados/binários se houver kanji no path. O
  nome bonito com kanji fica só na UI, nunca no filesystem.
- **`PYTHONUTF8=1`** é definido antes de criar o processo no Windows, para não
  mascarar erros reais com problemas de encoding (cp1252).
- A venv é chamada **diretamente pelo caminho do interpretador**
  (`.venv/Scripts/python.exe`), sem precisar "ativar".

---

## ⚖️ Aviso

Ferramenta de uso pessoal para leitura offline de conteúdo adquirido/acessível
legalmente. Respeite os termos de uso dos sites de origem e as leis de direito
autoral aplicáveis.

---

## 📄 Licença

Distribuído sob a licença **MIT**. Veja [LICENSE](LICENSE) para mais detalhes.
