extends CanvasLayer

## Leitor de CBZ (zip de imagens) in-app. Instanciado como overlay sobre a tela
## atual. Abre um .cbz, ordena as páginas e mostra uma por vez com navegação.
## Não descompacta em disco — lê cada página do zip sob demanda (ZIPReader).

signal closed

@onready var page_texture: TextureRect = %PageTexture
@onready var page_label: Label = %PageLabel
@onready var prev_button: Button = %PrevButton
@onready var next_button: Button = %NextButton
@onready var close_button: Button = %CloseButton

const IMAGE_EXTS := ["jpg", "jpeg", "png", "webp", "bmp"]

var _zip: ZIPReader
var _pages: Array[String] = []
var _index: int = 0


func _ready() -> void:
	prev_button.pressed.connect(_prev)
	next_button.pressed.connect(_next)
	close_button.pressed.connect(close)


## Abre um arquivo .cbz e mostra a primeira página. Retorna false se falhar.
func open_cbz(path: String) -> bool:
	_zip = ZIPReader.new()
	if _zip.open(path) != OK:
		push_error("Reader: não consegui abrir %s" % path)
		return false

	_pages.clear()
	for entry in _zip.get_files():
		if entry.get_extension().to_lower() in IMAGE_EXTS:
			_pages.append(entry)
	# Ordenação natural: 1, 2, 10 em vez de 1, 10, 2.
	_pages.sort_custom(func(a, b): return a.naturalnocasecmp_to(b) < 0)

	if _pages.is_empty():
		push_error("Reader: nenhuma imagem em %s" % path)
		return false

	_index = 0
	_show_page()
	return true


func _show_page() -> void:
	var entry := _pages[_index]
	var data := _zip.read_file(entry)
	var img := Image.new()
	var err := _decode(img, entry, data)
	if err == OK:
		page_texture.texture = ImageTexture.create_from_image(img)
	else:
		push_error("Reader: falha decodificando %s" % entry)
	page_label.text = "%d / %d" % [_index + 1, _pages.size()]
	prev_button.disabled = _index == 0
	next_button.disabled = _index == _pages.size() - 1


func _decode(img: Image, entry: String, data: PackedByteArray) -> int:
	match entry.get_extension().to_lower():
		"png": return img.load_png_from_buffer(data)
		"webp": return img.load_webp_from_buffer(data)
		"bmp": return img.load_bmp_from_buffer(data)
		_: return img.load_jpg_from_buffer(data)   # jpg/jpeg e fallback


func _prev() -> void:
	if _index > 0:
		_index -= 1
		_show_page()


func _next() -> void:
	if _index < _pages.size() - 1:
		_index += 1
		_show_page()


func close() -> void:
	if _zip:
		_zip.close()
	closed.emit()
	queue_free()


func _unhandled_input(event: InputEvent) -> void:
	if event.is_action_pressed("ui_left"):
		_prev()
	elif event.is_action_pressed("ui_right"):
		_next()
	elif event.is_action_pressed("ui_cancel"):
		close()
