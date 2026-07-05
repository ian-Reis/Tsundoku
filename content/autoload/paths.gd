extends Node

## Autoload "Paths" — resolve todos os caminhos do runtime Python num lugar só.
## Resolve o interpretador da venv conforme o SO (Windows usa Scripts/, Linux/
## macOS usam bin/), o que deixa o app rodar tanto no Windows quanto no Arch sem
## caminhos hardcoded espalhados.

var runtime_dir: String
var temp_dir: String
var output_dir: String
var python_exe: String      # venv enxuta do Tsundoku
var aio_python: String      # venv própria do AIO (deps pesadas)


func _ready() -> void:
	runtime_dir = ProjectSettings.globalize_path("res://content/runtime")
	temp_dir = runtime_dir.path_join("temp")
	output_dir = runtime_dir.path_join("downloads")
	python_exe = _venv_python(runtime_dir.path_join(".venv"))
	aio_python = _venv_python(runtime_dir.path_join("aio/.venv"))


## Caminho absoluto de um script na raiz do runtime.
func script(name: String) -> String:
	return runtime_dir.path_join(name)


func _venv_python(venv_dir: String) -> String:
	if OS.get_name() == "Windows":
		return venv_dir.path_join("Scripts/python.exe")
	return venv_dir.path_join("bin/python")
