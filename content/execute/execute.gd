extends Control

## VIEW da tela de download. Lê os inputs, cria um Job, entrega ao ProcessManager
## e reage aos sinais dele atualizando os cards (Task). Não conhece fila, spawn,
## polling nem status file — isso tudo vive no autoload ProcessManager.
##
## Textos vêm do autoload I18n (PT-BR/EN). Dois seletores: idioma da interface
## (%UiLangOption) e idioma do conteúdo baixado (%ContentLangOption).

@onready var url_text_edit: TextEdit = %UrlTextEdit
@onready var volumes_text_edit: TextEdit = %VolumesTextEdit
@onready var chapters_text_edit: TextEdit = %ChaptersTextEdit
@onready var option_button: OptionButton = %OptionButton
@onready var content_lang_option: OptionButton = %ContentLangOption
@onready var ui_lang_option: OptionButton = %UiLangOption
@onready var download_button: Button = %DownloadButton
@onready var setup_button: Button = %SetupButton
@onready var status_label: Label = %StatusLabel
@onready var queue_vbox_container: VBoxContainer = %QueueVBoxContainer

const TASK: PackedScene = preload("res://content/execute/task.tscn")

# Códigos de idioma de conteúdo, na ordem dos itens do %ContentLangOption.
const CONTENT_LANGS := ["pt-br", "en"]

# Usado quando o campo de capítulos fica vazio: baixa a série inteira.
const DEFAULT_CHAPTERS := "all"

# Cores de estado dos cards.
const COLOR_QUEUED := Color(0.7, 0.7, 0.7)      # cinza — esperando na fila
const COLOR_ACTIVE := Color(1, 1, 1)            # branco — rodando
const COLOR_DONE := Color(0.55, 0.9, 0.55)      # verde — concluído
const COLOR_ERROR := Color(0.95, 0.5, 0.5)      # vermelho — erro/cancelado

# Mapeamentos job.id -> estado da view.
var _cards: Dictionary = {}     # job.id -> Task (nó do card)
var _jobs: Dictionary = {}      # job.id -> Job (pra cancelar)
var _started: Dictionary = {}   # job.id -> true (já saiu da fila e começou)


func _ready() -> void:
	download_button.pressed.connect(_on_download_pressed)
	setup_button.pressed.connect(_on_setup_pressed)
	ui_lang_option.item_selected.connect(_on_ui_lang_selected)
	I18n.language_changed.connect(_on_language_changed)

	ProcessManager.job_started.connect(_on_job_started)
	ProcessManager.job_progress.connect(_on_job_progress)
	ProcessManager.job_finished.connect(_on_job_finished)
	ProcessManager.job_failed.connect(_on_job_failed)
	ProcessManager.job_cancelled.connect(_on_job_cancelled)

	RuntimeSetup.progress.connect(_on_setup_progress)
	RuntimeSetup.finished.connect(_on_setup_finished)
	RuntimeSetup.failed.connect(_on_setup_failed)

	ui_lang_option.selected = I18n.locale_index()
	_apply_language()
	_refresh_setup_state()


# ---------------------------------------------------------------------------
# Instalação do runtime
# ---------------------------------------------------------------------------
## Mostra o botão "Instalar runtime" (e bloqueia o Download) enquanto o Python
## do runtime não existe. Some quando estiver instalado.
func _refresh_setup_state() -> void:
	var needs := RuntimeSetup.needs_setup()
	setup_button.visible = needs
	download_button.disabled = needs or RuntimeSetup.is_running()
	if needs and not RuntimeSetup.is_running():
		_set_status(I18n.t("setup_needed"))


func _on_setup_pressed() -> void:
	setup_button.disabled = true
	download_button.disabled = true
	RuntimeSetup.install()


func _on_setup_progress(percent: int, message: String) -> void:
	_set_status(I18n.t("setup_progress", [message, percent]))


func _on_setup_finished() -> void:
	setup_button.disabled = false
	_set_status(I18n.t("setup_done"))
	_refresh_setup_state()


func _on_setup_failed(message: String) -> void:
	setup_button.disabled = false
	_set_status(I18n.t("setup_failed", [message]))
	_refresh_setup_state()


# ---------------------------------------------------------------------------
# Idioma
# ---------------------------------------------------------------------------
func _on_ui_lang_selected(index: int) -> void:
	if index >= 0 and index < I18n.LOCALES.size():
		I18n.set_locale(I18n.LOCALES[index])


func _on_language_changed(_locale: String) -> void:
	_apply_language()
	if not RuntimeSetup.is_running():
		_refresh_setup_state()


## (Re)aplica os textos estáticos no idioma atual. Cards já na fila mantêm o
## texto que tinham; só as próximas atualizações saem traduzidas.
func _apply_language() -> void:
	download_button.text = I18n.t("app_download")
	setup_button.text = I18n.t("app_install")
	url_text_edit.placeholder_text = I18n.t("ph_url")
	volumes_text_edit.placeholder_text = I18n.t("ph_volumes")
	chapters_text_edit.placeholder_text = I18n.t("ph_chapters")

	for d in Sources.DEFS:
		option_button.set_item_text(d.id, Sources.label(d.id))

	if not ProcessManager.is_busy() and ProcessManager.queue_size() == 0:
		_set_status(I18n.t("status_intro"))


func _content_lang() -> String:
	var idx := content_lang_option.selected
	return CONTENT_LANGS[idx] if idx >= 0 and idx < CONTENT_LANGS.size() else "pt-br"


# ---------------------------------------------------------------------------
# Enfileirar
# ---------------------------------------------------------------------------
func _on_download_pressed() -> void:
	var url := url_text_edit.text.strip_edges()
	if url == "":
		_set_status(I18n.t("status_need_url"))
		return

	var source_id := option_button.selected
	var def := Sources.get_def(source_id)
	if def.is_empty():
		return
	if not FileAccess.file_exists(Paths.script(def.script)):
		_set_status(I18n.t("status_source_missing", [def.script]))
		return

	# Campos opcionais. Capítulos vazio = série inteira; volumes vazio = omitido.
	var chapters := chapters_text_edit.text.strip_edges()
	if chapters == "":
		chapters = DEFAULT_CHAPTERS
	var volumes := volumes_text_edit.text.strip_edges()

	# submit() atribui job.id e adia o start (call_deferred), então criamos o
	# card logo depois, já com o id certo, antes do job_started disparar.
	var job := Job.new(url, source_id, chapters, volumes, _content_lang())
	ProcessManager.submit(job)
	_spawn_card(job)

	url_text_edit.text = ""
	volumes_text_edit.text = ""
	chapters_text_edit.text = ""
	_set_status(I18n.t("status_in_queue", [ProcessManager.queue_size() + (1 if ProcessManager.is_busy() else 0)]))


## Cria o card do job e registra os mapeamentos.
func _spawn_card(job: Job) -> void:
	var task = TASK.instantiate()  # sem tipo: chamadas dinâmicas (setup/set_state)
	queue_vbox_container.add_child(task)
	task.setup(_short_url(job.url))
	task.set_state(I18n.t("st_queued"), COLOR_QUEUED)
	task.set_meta("job_id", job.id)
	task.cancel_requested.connect(_on_card_cancel)
	_cards[job.id] = task
	_jobs[job.id] = job


# ---------------------------------------------------------------------------
# Sinais do ProcessManager
# ---------------------------------------------------------------------------
func _on_job_started(job: Job) -> void:
	_started[job.id] = true
	var card = _card_for(job)
	if card == null:
		return
	card.set_state(I18n.t("st_starting"), COLOR_ACTIVE)
	card.set_progress(0)
	_set_status(I18n.t("status_downloading_n", [ProcessManager.queue_size() + 1]))


func _on_job_progress(job: Job, data: Dictionary) -> void:
	var card = _card_for(job)
	if card == null:
		return
	match data.get(StatusContract.F_STATUS, ""):
		StatusContract.STARTING:
			card.set_state(I18n.t("st_preparing"), COLOR_ACTIVE)
		StatusContract.PREPARING:
			if data.has(StatusContract.F_TITLE):
				card.set_title(data[StatusContract.F_TITLE])
			card.set_state(I18n.t("st_chapters", [int(data.get(StatusContract.F_TOTAL, 0))]), COLOR_ACTIVE)
		StatusContract.DOWNLOADING:
			card.set_progress(data.get(StatusContract.F_PROGRESS, 0))
			card.set_state(I18n.t("st_chapter_of", [
				int(data.get(StatusContract.F_CURRENT, 0)),
				int(data.get(StatusContract.F_TOTAL, 0)),
			]), COLOR_ACTIVE)
		StatusContract.COOLDOWN:
			card.set_state(I18n.t("st_cooldown", [int(data.get(StatusContract.F_WAIT, 0))]), COLOR_QUEUED)


func _on_job_finished(job: Job, data: Dictionary) -> void:
	var card = _card_for(job)
	if card:
		card.set_progress(100)
		card.set_state(I18n.t("st_done", [int(data.get(StatusContract.F_DONE, 0))]), COLOR_DONE)
		card.disable_cancel()
	_drop(job.id)


func _on_job_failed(job: Job, message: String) -> void:
	var card = _card_for(job)
	if card:
		# Códigos internos do ProcessManager são localizados aqui; a mensagem de
		# erro vinda do script Python é mostrada como veio.
		var text := message
		match message:
			"err:spawn": text = I18n.t("st_no_start")
			"err:no_status": text = I18n.t("st_no_status")
		card.set_state(I18n.t("st_error", [text]), COLOR_ERROR)
		card.disable_cancel()
	_drop(job.id)


func _on_job_cancelled(job: Job) -> void:
	var card = _card_for(job)
	if card:
		if _started.has(job.id):
			# Estava rodando: mantém o card marcado como cancelado.
			card.set_state(I18n.t("st_cancelled"), COLOR_ERROR)
			card.disable_cancel()
		else:
			# Só esperava na fila: some com o card.
			card.queue_free()
	_drop(job.id)


func _on_card_cancel(task: Node) -> void:
	var id: int = task.get_meta("job_id", -1)
	if _jobs.has(id):
		ProcessManager.cancel(_jobs[id])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
func _card_for(job: Job):
	return _cards.get(job.id)


func _drop(id: int) -> void:
	_cards.erase(id)
	_jobs.erase(id)
	_started.erase(id)


## Encurta a URL pra um nome provisório legível no card até o título real chegar.
func _short_url(url: String) -> String:
	var trimmed := url.trim_suffix("/")
	var last := trimmed.get_slice("/", trimmed.get_slice_count("/") - 1)
	return last if last != "" else url


func _set_status(text: String) -> void:
	if status_label:
		status_label.text = text
