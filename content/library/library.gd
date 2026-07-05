extends CanvasLayer

## Tela de Biblioteca (overlay). Varre downloads/ via Library.scan() e lista os
## títulos com seus capítulos. Clicar num capítulo: CBZ abre no leitor in-app;
## PDF/EPUB abre no app do sistema.

signal closed

const READER := preload("res://content/reader/reader.tscn")
const LIBRARY_ITEM := preload("res://content/library/library_item.tscn")

@onready var title_label: Label = %TitleLabel
@onready var refresh_button: Button = %RefreshButton
@onready var close_button: Button = %CloseButton
@onready var list_container: VBoxContainer = %ListContainer
@onready var empty_label: Label = %EmptyLabel


func _ready() -> void:
	title_label.text = I18n.t("app_library")
	refresh_button.text = I18n.t("app_refresh")
	empty_label.text = I18n.t("lib_empty")
	refresh_button.pressed.connect(refresh)
	close_button.pressed.connect(close)
	refresh()


func refresh() -> void:
	for child in list_container.get_children():
		child.queue_free()

	var items := Library.scan()
	empty_label.visible = items.is_empty()
	for item in items:
		_add_title(item)


func _add_title(item: Dictionary) -> void:
	var node = LIBRARY_ITEM.instantiate()  # sem tipo: chamadas dinâmicas (setup)
	list_container.add_child(node)
	node.chapter_selected.connect(_open_chapter)
	node.setup(item)


func _open_chapter(ch: Dictionary) -> void:
	if Library.is_readable_in_app(ch.ext):
		var reader = READER.instantiate()
		get_tree().root.add_child(reader)
		if not reader.open_cbz(ch.path):
			reader.queue_free()
	else:
		# PDF/EPUB → leitor padrão do SO.
		OS.shell_open(ch.path)


func close() -> void:
	closed.emit()
	queue_free()


func _unhandled_input(event: InputEvent) -> void:
	if event.is_action_pressed("ui_cancel"):
		close()
