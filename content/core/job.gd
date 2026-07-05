class_name Job
extends RefCounted

## Modelo de um item de download. Puro dado — não conhece UI. O ProcessManager
## atribui id/status_file e o executa; a view mapeia job.id -> card por fora.

var id: int = -1
var url: String
var source_id: int
var chapters: String = "all"
var volumes: String = ""
var lang: String = "pt-br"
var status_file: String = ""
var pid: int = -1


func _init(p_url: String, p_source_id: int, p_chapters: String, p_volumes: String, p_lang: String) -> void:
	url = p_url
	source_id = p_source_id
	chapters = p_chapters
	volumes = p_volumes
	lang = p_lang


## Monta a linha de comando (sem o interpretador — esse é o Paths.python_exe).
## Toda a lógica "qual flag para qual fonte" vem da def em Sources, não de ifs.
func build_args() -> PackedStringArray:
	var d := Sources.get_def(source_id)
	var args := PackedStringArray([
		Paths.script(d.script),
		url,
		"--chapters", chapters,
		"--output", Paths.output_dir,
		"--status-file", status_file,
	])
	# --volumes só onde a fonte suporta e o usuário preencheu.
	if d.get("volumes", false) and volumes != "":
		args.append("--volumes")
		args.append(volumes)
	# Idioma de conteúdo, com o nome de flag específico da fonte.
	var lang_flag: String = d.get("lang_flag", "")
	if lang_flag != "":
		args.append(lang_flag)
		args.append(lang)
	# Fontes que rodam via wrapper do AIO precisam apontar a venv do AIO.
	if d.get("aio", false):
		args.append("--aio-python")
		args.append(Paths.aio_python)
	return args
