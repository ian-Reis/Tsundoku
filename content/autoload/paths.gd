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
	_compute()


## Recalcula os caminhos. Chamado após instalar o runtime embeddable, pois aí o
## python.exe passa a existir num lugar que não existia no boot.
func refresh() -> void:
	_compute()


func _compute() -> void:
	runtime_dir = _resolve_runtime_dir()
	temp_dir = runtime_dir.path_join("temp")
	# downloads/ fica na RAIZ, ao lado de runtime/ (não dentro), pra organização:
	# <raiz>/runtime/  e  <raiz>/downloads/<fonte>/
	output_dir = runtime_dir.get_base_dir().path_join("downloads")
	python_exe = _resolve_python(runtime_dir)
	aio_python = _resolve_python(runtime_dir.path_join("aio"))


## No editor, o runtime fica em res://content/runtime. Num build exportado, res://
## aponta pro .pck (não é arquivo real) — então o runtime é enviado como pasta
## solta AO LADO do executável e resolvido a partir dele.
func _resolve_runtime_dir() -> String:
	if OS.has_feature("editor"):
		return ProjectSettings.globalize_path("res://content/runtime")
	return OS.get_executable_path().get_base_dir().path_join("runtime")


## Caminho absoluto de um script na raiz do runtime.
func script(name: String) -> String:
	return runtime_dir.path_join(name)


## Resolve o interpretador Python para um "root" (a pasta que contém python/
## embeddable e/ou .venv). Prioriza o embeddable autocontido; se não existir, cai
## pra venv. No Windows o embeddable fica em <root>/python/python.exe.
func _resolve_python(root: String) -> String:
	if OS.get_name() == "Windows":
		var embed := root.path_join("python/python.exe")
		if FileAccess.file_exists(embed):
			return embed
		return root.path_join(".venv/Scripts/python.exe")
	# Linux/macOS: embeddable é conceito Windows; usa a venv.
	var embed_nix := root.path_join("python/bin/python")
	if FileAccess.file_exists(embed_nix):
		return embed_nix
	return root.path_join(".venv/bin/python")
