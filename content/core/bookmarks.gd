class_name Bookmarks
extends RefCounted

## Guarda a última página lida de cada CBZ, pra retomar de onde parou.
## Persistido em user://bookmarks.cfg. A chave é o caminho do arquivo relativo à
## pasta downloads (estável entre dev e build, e se a pasta do app for movida).

const PATH := "user://bookmarks.cfg"
const SECTION := "pages"


## Página salva (0-based) do CBZ. 0 se não houver marcação.
static func get_page(cbz_path: String) -> int:
	var cfg := ConfigFile.new()
	if cfg.load(PATH) != OK:
		return 0
	return int(cfg.get_value(SECTION, _key(cbz_path), 0))


## Salva a página atual (0-based) do CBZ.
static func set_page(cbz_path: String, page: int) -> void:
	var cfg := ConfigFile.new()
	cfg.load(PATH)  # ignora erro: se não existe, começa vazio
	cfg.set_value(SECTION, _key(cbz_path), page)
	cfg.save(PATH)


## Chave estável: caminho relativo à pasta downloads (ou absoluto como fallback).
static func _key(cbz_path: String) -> String:
	var root := Paths.output_dir
	if cbz_path.begins_with(root):
		return cbz_path.substr(root.length()).lstrip("/\\")
	return cbz_path
