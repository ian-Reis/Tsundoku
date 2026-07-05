extends Control

## Tela de download com FILA. O usuário cola uma URL, escolhe o tipo/idioma e
## clica Baixar; um card (Task) é criado no QueueVBoxContainer. Os jobs rodam um
## de cada vez. Cada job tem seu próprio status_<id>.json — assim jobs futuros/
## paralelos nunca sobrescrevem o mesmo arquivo. O Python roda como subprocesso
## (create_process, não bloqueia a UI) e reporta progresso via status file, lido
## aqui por polling num Timer.
##
## Textos vêm do autoload I18n (PT-BR/EN). Há dois seletores de idioma: o da
## interface (%UiLangOption) e o do conteúdo baixado (%ContentLangOption).

@onready var url_text_edit: TextEdit = %UrlTextEdit
@onready var volumes_text_edit: TextEdit = %VolumesTextEdit
@onready var chapters_text_edit: TextEdit = %ChaptersTextEdit
@onready var option_button: OptionButton = %OptionButton
@onready var content_lang_option: OptionButton = %ContentLangOption
@onready var ui_lang_option: OptionButton = %UiLangOption
@onready var download_button: Button = %DownloadButton
@onready var status_label: Label = %StatusLabel
@onready var queue_vbox_container: VBoxContainer = %QueueVBoxContainer

const TASK: PackedScene = preload("res://content/execute/task.tscn")

# Índices do OptionButton (tipo + site alvo). MangaFire é servido pelo
# AIO-Webtoon-Downloader (via wrapper); os demais têm scraper próprio.
const TYPE_NOVEL := 0        # CentralNovel
const TYPE_MANGA := 1        # MangaLivre
const TYPE_MANGAFIRE := 2    # MangaFire (via AIO)
const TYPE_MANGADEX := 3     # MangaDex

# Códigos de idioma de conteúdo, na ordem dos itens do %ContentLangOption.
const CONTENT_LANGS := ["pt-br", "en"]

# Usado quando o campo de capítulos fica vazio: baixa a série inteira.
const DEFAULT_CHAPTERS := "all"

# Cores de estado dos cards.
const COLOR_QUEUED := Color(0.7, 0.7, 0.7)      # cinza — esperando na fila
const COLOR_ACTIVE := Color(1, 1, 1)            # branco — rodando
const COLOR_DONE := Color(0.55, 0.9, 0.55)      # verde — concluído
const COLOR_ERROR := Color(0.95, 0.5, 0.5)      # vermelho — erro/cancelado

# Caminhos do runtime. globalize_path converte res:// pra caminho absoluto do
# sistema — o processo Python roda fora do Godot e não entende res://.
var runtime_dir: String = ProjectSettings.globalize_path("res://content/runtime")
var python_exe: String = runtime_dir.path_join(".venv/Scripts/python.exe")
var script_novel: String = runtime_dir.path_join("centralnovel_dlv7.py")
var script_manga: String = runtime_dir.path_join("mangalivre_dlv4.py")
var script_mangadex: String = runtime_dir.path_join("mangadex_dl.py")
# AIO roda via wrapper (stdlib) chamado pela nossa venv, mas o aio-dl.py em si
# precisa da venv própria dele (deps pesadas — ver README).
var script_aio: String = runtime_dir.path_join("aio_dl_wrapper.py")
var aio_python: String = runtime_dir.path_join("aio/.venv/Scripts/python.exe")
var temp_dir: String = runtime_dir.path_join("temp")
var output_dir: String = runtime_dir.path_join("downloads")

# Fila e estado de execução.
var _queue: Array[Dictionary] = []   # jobs esperando
var _current: Dictionary = {}        # job rodando agora ({} = nenhum)
var _next_id: int = 0                # contador pra status_<id>.json único
var _poll_timer: Timer


func _ready() -> void:
	download_button.pressed.connect(_on_download_pressed)
	ui_lang_option.item_selected.connect(_on_ui_lang_selected)
	I18n.language_changed.connect(_on_language_changed)

	_poll_timer = Timer.new()
	_poll_timer.wait_time = 0.3
	_poll_timer.timeout.connect(_poll_status)
	add_child(_poll_timer)

	ui_lang_option.selected = I18n.locale_index()
	_apply_language()


# ---------------------------------------------------------------------------
# Idioma
# ---------------------------------------------------------------------------
func _on_ui_lang_selected(index: int) -> void:
	if index >= 0 and index < I18n.LOCALES.size():
		I18n.set_locale(I18n.LOCALES[index])


func _on_language_changed(_locale: String) -> void:
	_apply_language()


## (Re)aplica os textos estáticos no idioma atual. Cards já na fila mantêm o
## texto que tinham; só as próximas atualizações saem traduzidas.
func _apply_language() -> void:
	download_button.text = I18n.t("app_download")
	url_text_edit.placeholder_text = I18n.t("ph_url")
	volumes_text_edit.placeholder_text = I18n.t("ph_volumes")
	chapters_text_edit.placeholder_text = I18n.t("ph_chapters")

	# Rótulo = palavra traduzida do tipo + nome do site (fixo).
	option_button.set_item_text(TYPE_NOVEL, "%s (CentralNovel)" % I18n.t("type_novel"))
	option_button.set_item_text(TYPE_MANGA, "%s (MangaLivre)" % I18n.t("type_manga"))
	option_button.set_item_text(TYPE_MANGAFIRE, "%s (MangaFire)" % I18n.t("type_manga"))
	option_button.set_item_text(TYPE_MANGADEX, "%s (MangaDex)" % I18n.t("type_manga"))

	if _current.is_empty() and _queue.is_empty():
		_set_status(I18n.t("status_intro"))


func _content_lang() -> String:
	var idx := content_lang_option.selected
	return CONTENT_LANGS[idx] if idx >= 0 and idx < CONTENT_LANGS.size() else "pt-br"


# ---------------------------------------------------------------------------
# Fila
# ---------------------------------------------------------------------------
func _on_download_pressed() -> void:
	var url := url_text_edit.text.strip_edges()
	if url == "":
		_set_status(I18n.t("status_need_url"))
		return

	var type := option_button.selected
	if type == TYPE_MANGA and not FileAccess.file_exists(script_manga):
		_set_status(I18n.t("status_manga_unavailable"))
		return
	if type == TYPE_MANGAFIRE and not FileAccess.file_exists(script_aio):
		_set_status(I18n.t("status_aio_missing"))
		return
	if type == TYPE_MANGADEX and not FileAccess.file_exists(script_mangadex):
		_set_status(I18n.t("status_mangadex_missing"))
		return

	# Campos opcionais. Capítulos vazio = série inteira; volumes vazio = omitido.
	var chapters := chapters_text_edit.text.strip_edges()
	if chapters == "":
		chapters = DEFAULT_CHAPTERS
	var volumes := volumes_text_edit.text.strip_edges()

	_enqueue(url, type, chapters, volumes, _content_lang())
	url_text_edit.text = ""
	volumes_text_edit.text = ""
	chapters_text_edit.text = ""


func _enqueue(url: String, type: int, chapters: String, volumes: String, lang: String) -> void:
	var id := _next_id
	_next_id += 1

	var task = TASK.instantiate()  # sem tipo: chamadas dinâmicas (setup/set_state)
	queue_vbox_container.add_child(task)
	task.setup(_short_url(url))
	task.set_state(I18n.t("st_queued"), COLOR_QUEUED)
	task.cancel_requested.connect(_on_cancel_requested)

	var job := {
		"id": id,
		"url": url,
		"type": type,
		"chapters": chapters,
		"volumes": volumes,
		"lang": lang,
		"task": task,
		"status_file": temp_dir.path_join("status_%d.json" % id),
		"pid": -1,
	}
	_queue.append(job)
	_set_status(I18n.t("status_in_queue", [_queue.size() + (1 if not _current.is_empty() else 0)]))
	_try_start_next()


## Se ninguém está rodando e há job na fila, inicia o próximo.
func _try_start_next() -> void:
	if not _current.is_empty():
		return
	if _queue.is_empty():
		_set_status(I18n.t("status_queue_empty"))
		return
	_current = _queue.pop_front()
	_start_job(_current)


func _start_job(job: Dictionary) -> void:
	# Garante a pasta temp e limpa qualquer status file antigo desse id, senão
	# o primeiro poll pode ler o resultado de uma rodada anterior.
	DirAccess.make_dir_recursive_absolute(temp_dir)
	if FileAccess.file_exists(job["status_file"]):
		DirAccess.remove_absolute(job["status_file"])

	# Higiene de encoding no Windows (a saga do cp1252).
	OS.set_environment("PYTHONUTF8", "1")

	var script: String
	match job["type"]:
		TYPE_NOVEL:     script = script_novel
		TYPE_MANGAFIRE: script = script_aio
		TYPE_MANGADEX:  script = script_mangadex
		_:              script = script_manga

	var args := [
		script,
		job["url"],
		"--chapters", job["chapters"],
		"--output", output_dir,
		"--status-file", job["status_file"],
	]
	# --volumes é opcional e SÓ existe no script de novel (mangá/AIO/MangaDex não).
	if job["type"] == TYPE_NOVEL and job["volumes"] != "":
		args.append("--volumes")
		args.append(job["volumes"])
	# Idioma de conteúdo: MangaDex usa --lang; o wrapper do AIO usa --language.
	# Novel (só pt) e mangalivre (site pt-br) não têm esse argumento.
	if job["type"] == TYPE_MANGADEX:
		args.append("--lang")
		args.append(job["lang"])
	elif job["type"] == TYPE_MANGAFIRE:
		args.append("--language")
		args.append(job["lang"])
		# O wrapper do AIO precisa saber qual Python roda o aio-dl.py (venv própria).
		args.append("--aio-python")
		args.append(aio_python)

	# create_process NÃO bloqueia — retorna o PID e o Python roda em paralelo.
	job["pid"] = OS.create_process(python_exe, args)
	if job["pid"] == -1:
		job["task"].set_state(I18n.t("st_no_start"), COLOR_ERROR)
		push_error("create_process falhou: %s" % python_exe)
		_current = {}
		_try_start_next()
		return

	job["task"].set_state(I18n.t("st_starting"), COLOR_ACTIVE)
	job["task"].set_progress(0)
	_set_status(I18n.t("status_downloading_n", [_queue.size() + 1]))
	_poll_timer.start()


func _poll_status() -> void:
	if _current.is_empty():
		_poll_timer.stop()
		return

	var status_file: String = _current["status_file"]

	if not FileAccess.file_exists(status_file):
		# O processo pode ter começado mas ainda não escreveu nada. Se ele já
		# morreu sem gerar arquivo, algo deu errado na inicialização.
		if not OS.is_process_running(_current["pid"]):
			_on_process_gone_without_status()
		return

	var file := FileAccess.open(status_file, FileAccess.READ)
	if file == null:
		return  # arquivo sendo reescrito nesse instante; tenta no próximo tick

	var content := file.get_as_text()
	file.close()

	var json := JSON.new()
	if json.parse(content) != OK:
		return  # JSON incompleto por race rara; ignora e tenta de novo

	_apply_status(_current, json.data)


func _apply_status(job: Dictionary, data: Dictionary) -> void:
	var task = job["task"]
	var status: String = data.get("status", "")

	match status:
		"starting":
			task.set_state(I18n.t("st_preparing"), COLOR_ACTIVE)

		"preparing":
			if data.has("title"):
				task.set_title(data["title"])
			task.set_state(I18n.t("st_chapters", [int(data.get("total_chapters", 0))]), COLOR_ACTIVE)

		"downloading":
			task.set_progress(data.get("progress", 0))
			task.set_state(I18n.t("st_chapter_of", [
				int(data.get("current_chapter", 0)),
				int(data.get("total_chapters", 0)),
			]), COLOR_ACTIVE)

		"cooldown":
			# Estado precioso: mostra que está esperando, não travado.
			task.set_state(I18n.t("st_cooldown", [int(data.get("wait_seconds", 0))]), COLOR_QUEUED)

		"done":
			task.set_progress(100)
			task.set_state(I18n.t("st_done", [int(data.get("done", 0))]), COLOR_DONE)
			task.disable_cancel()
			_finish_current()

		"error":
			task.set_state(I18n.t("st_error", [data.get("message", "?")]), COLOR_ERROR)
			task.disable_cancel()
			_finish_current()

		"cancelled":
			task.set_state(I18n.t("st_cancelled"), COLOR_ERROR)
			task.disable_cancel()
			_finish_current()


func _finish_current() -> void:
	_poll_timer.stop()
	_current = {}
	_try_start_next()


func _on_process_gone_without_status() -> void:
	# O Python morreu sem escrever nem um status — provável crash na
	# inicialização (venv errada, import faltando, etc.).
	if not _current.is_empty():
		_current["task"].set_state(I18n.t("st_no_status"), COLOR_ERROR)
		_current["task"].disable_cancel()
	push_error("Processo Python encerrou sem escrever status. Rode no terminal pra ver o erro.")
	_finish_current()


## Chamado quando o usuário clica no X de um card. Se o job está rodando, mata o
## processo; se está só esperando na fila, remove o card e descarta o job.
func _on_cancel_requested(task: Node) -> void:
	# Caso 1: é o job que está rodando agora.
	if not _current.is_empty() and _current["task"] == task:
		if _current["pid"] != -1 and OS.is_process_running(_current["pid"]):
			OS.kill(_current["pid"])
		task.set_state(I18n.t("st_cancelled"), COLOR_ERROR)
		task.disable_cancel()
		_finish_current()
		return

	# Caso 2: é um job ainda na fila (não iniciado). Remove e libera o card.
	for i in range(_queue.size()):
		if _queue[i]["task"] == task:
			_queue.remove_at(i)
			task.queue_free()
			_set_status(I18n.t("status_removed"))
			return


## Encurta a URL pra um nome provisório legível no card até o título real chegar.
func _short_url(url: String) -> String:
	var trimmed := url.trim_suffix("/")
	var last := trimmed.get_slice("/", trimmed.get_slice_count("/") - 1)
	return last if last != "" else url


func _set_status(text: String) -> void:
	if status_label:
		status_label.text = text
