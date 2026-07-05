class_name StatusContract
extends RefCounted

## Fonte única do "protocolo" status.json entre os scripts Python e o Godot.
## Centraliza os nomes de estado e de campo (antes eram strings soltas espalhadas)
## e a leitura robusta do arquivo (tolerante a escrita concorrente / JSON parcial).

# Estados
const STARTING := "starting"
const PREPARING := "preparing"
const DOWNLOADING := "downloading"
const COOLDOWN := "cooldown"
const DONE := "done"
const ERROR := "error"
const CANCELLED := "cancelled"

# Campos
const F_STATUS := "status"
const F_PROGRESS := "progress"
const F_TITLE := "title"
const F_TOTAL := "total_chapters"
const F_CURRENT := "current_chapter"
const F_WAIT := "wait_seconds"
const F_DONE := "done"
const F_MESSAGE := "message"


## Lê e parseia o status file. Retorna {} se o arquivo não existe, está sendo
## reescrito nesse instante, ou o JSON veio incompleto (race raro) — o chamador
## simplesmente tenta de novo no próximo tick.
static func read(status_file: String) -> Dictionary:
	if not FileAccess.file_exists(status_file):
		return {}
	var file := FileAccess.open(status_file, FileAccess.READ)
	if file == null:
		return {}
	var content := file.get_as_text()
	file.close()
	# Descarta um BOM UTF-8 no início (alguns escritores no Windows o adicionam),
	# senão o JSON.parse falha no primeiro caractere.
	if content.length() > 0 and content.unicode_at(0) == 0xFEFF:
		content = content.substr(1)
	var json := JSON.new()
	if json.parse(content) != OK:
		return {}
	var data = json.data
	return data if data is Dictionary else {}


static func is_terminal(status: String) -> bool:
	return status == DONE or status == ERROR or status == CANCELLED
