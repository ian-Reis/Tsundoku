extends CanvasLayer

## Tela dedicada de "Juntar"/converter. Lista os títulos baixados (Library.scan()),
## deixa escolher o formato de saída e o nome, e roda o conversor certo:
##  - CBZ/EPUB/PDF  -> chapterbind_wrapper.py (junta capítulos num arquivo)
##  - XTC           -> cbz2xtc_wrapper.py (converte pro e-reader XTEink X4)
## Acompanha via status file, igual aos downloads.

signal closed

# Formato "xtc" é servido pelo cbz2xtc; os demais pelo chapterbind.
const FORMATS := ["cbz", "epub", "pdf", "xtc"]

@onready var screen_title: Label = %ScreenTitle
@onready var close_button: Button = %CloseButton
@onready var lbl_source: Label = %LblSource
@onready var title_option: OptionButton = %TitleOption
@onready var lbl_format: Label = %LblFormat
@onready var format_option: OptionButton = %FormatOption
@onready var lbl_name: Label = %LblName
@onready var name_edit: LineEdit = %NameEdit
@onready var bind_button: Button = %BindButton
@onready var status_label: Label = %StatusLabel
@onready var xtc_options_holder: ScrollContainer = %XtcOptionsHolder

const XTC_OPTIONS := preload("res://content/bind/xtc_options.tscn")

var _items: Array = []
var _pid: int = -1
var _status_file: String = ""
var _poll_timer: Timer
var _xtc_options: Node = null   # painel de opções XTC (instanciado no _ready)
var _is_xtc: bool = false       # o job atual é conversão XTC?


func _ready() -> void:
	screen_title.text = I18n.t("bind_title")
	lbl_source.text = I18n.t("bind_lbl_source")
	lbl_format.text = I18n.t("bind_lbl_format")
	lbl_name.text = I18n.t("bind_lbl_name")
	bind_button.text = I18n.t("app_bind")

	format_option.clear()
	for f in ["CBZ", "EPUB", "PDF", "XTC (e-reader)"]:
		format_option.add_item(f)
	format_option.selected = 0

	# Painel de opções XTC, embutido e escondido até o formato XTC ser escolhido.
	_xtc_options = XTC_OPTIONS.instantiate()
	xtc_options_holder.add_child(_xtc_options)
	xtc_options_holder.visible = false

	close_button.pressed.connect(close)
	bind_button.pressed.connect(_on_bind)
	title_option.item_selected.connect(_on_title_selected)
	format_option.item_selected.connect(_on_format_selected)

	_poll_timer = Timer.new()
	_poll_timer.wait_time = 0.3
	_poll_timer.timeout.connect(_poll)
	add_child(_poll_timer)

	_populate()


func _populate() -> void:
	_items = Library.scan()
	title_option.clear()
	for item in _items:
		title_option.add_item("%s / %s" % [item.source, item.title])

	if _items.is_empty():
		status_label.text = I18n.t("lib_empty")
		bind_button.disabled = true
	else:
		title_option.selected = 0
		_on_title_selected(0)


func _on_title_selected(idx: int) -> void:
	if idx >= 0 and idx < _items.size():
		name_edit.text = _items[idx].title


## Mostra o painel de opções XTC só quando o formato XTC está selecionado.
func _on_format_selected(index: int) -> void:
	xtc_options_holder.visible = index >= 0 and index < FORMATS.size() and FORMATS[index] == "xtc"


func _on_bind() -> void:
	var idx := title_option.selected
	if idx < 0 or idx >= _items.size():
		return

	var item: Dictionary = _items[idx]
	var fmt: String = FORMATS[format_option.selected]
	var book := name_edit.text.strip_edges()
	if book == "":
		book = item.title

	DirAccess.make_dir_recursive_absolute(Paths.temp_dir)
	_status_file = Paths.temp_dir.path_join("bind_status.json")
	if FileAccess.file_exists(_status_file):
		DirAccess.remove_absolute(_status_file)

	OS.set_environment("PYTHONUTF8", "1")

	_is_xtc = fmt == "xtc"
	var args: Array
	if _is_xtc:
		# cbz2xtc converte a PASTA de CBZs; saída em <pasta>/xtc_output/. Não usa
		# -o/-t (nomeia pelos próprios arquivos). As opções do painel viram flags.
		args = [
			Paths.script("cbz2xtc_wrapper.py"),
			"--status-file", _status_file,
			item.path,
		]
		args += _xtc_options.build_args()
	else:
		# Saída AO LADO da pasta de capítulos (fora dela, pra o chapterbind não
		# incluir a própria saída numa segunda passada).
		var out_file: String = item.path.get_base_dir().path_join("%s.%s" % [_safe_filename(book), fmt])
		args = [
			Paths.script("chapterbind_wrapper.py"),
			"--status-file", _status_file,
			item.path,
			"-f", fmt,
			"-o", out_file,
			"-t", book,
		]

	_pid = OS.create_process(Paths.python_exe, args)
	if _pid == -1:
		status_label.text = I18n.t("bind_error", ["create_process"])
		return

	bind_button.disabled = true
	status_label.text = _working_text()
	_poll_timer.start()


## "Convertendo pro e-reader..." no XTC; "Juntando..." nos demais.
func _working_text() -> String:
	return I18n.t("xtc_working") if _is_xtc else I18n.t("bind_working")


func _poll() -> void:
	if _pid == -1:
		_poll_timer.stop()
		return

	var data := StatusContract.read(_status_file)
	if data.is_empty():
		if not OS.is_process_running(_pid):
			_finish()
			status_label.text = I18n.t("bind_error", ["sem status"])
		return

	match data.get("status", ""):
		"done":
			_finish()
			status_label.text = I18n.t("bind_done", [data.get("output_path", "")])
		"error":
			_finish()
			status_label.text = I18n.t("bind_error", [data.get("message", "?")])
		_:
			status_label.text = _working_text()


func _finish() -> void:
	_poll_timer.stop()
	_pid = -1
	bind_button.disabled = false


## Remove caracteres inválidos de nome de arquivo (o : do "Mushoku Tensei:" etc.).
func _safe_filename(name: String) -> String:
	var out := name
	for c in "<>:\"/\\|?*":
		out = out.replace(c, "_")
	return out.strip_edges()


func close() -> void:
	closed.emit()
	queue_free()


func _unhandled_input(event: InputEvent) -> void:
	if event.is_action_pressed("ui_cancel"):
		close()
