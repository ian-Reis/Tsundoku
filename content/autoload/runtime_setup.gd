extends Node

## Autoload "RuntimeSetup" — instala o runtime Python embeddable a partir do
## próprio app, chamando setup_embed.ps1 via powershell.exe (que existe em todo
## Windows, então não precisa de Python pré-instalado pra o bootstrap). Reporta
## progresso por status file, no mesmo espírito dos downloads.

signal progress(percent: int, message: String)
signal finished()
signal failed(message: String)

var _pid: int = -1
var _status_file: String = ""
var _timer: Timer
var _running: bool = false
var _last_message: String = ""


func _ready() -> void:
	_timer = Timer.new()
	_timer.wait_time = 0.4
	_timer.timeout.connect(_poll)
	add_child(_timer)


## True se o interpretador enxuto ainda não existe (precisa instalar).
func needs_setup() -> bool:
	return not FileAccess.file_exists(Paths.python_exe)


func is_running() -> bool:
	return _running


## Dispara a instalação. aio=true monta o embeddable pesado do AIO.
func install(aio: bool = false) -> void:
	if _running:
		return
	if OS.get_name() != "Windows":
		failed.emit("Instalador automático só no Windows (no Linux, crie a venv/embeddable manualmente).")
		return

	var script := Paths.runtime_dir.path_join("setup_embed.ps1")
	if not FileAccess.file_exists(script):
		failed.emit("setup_embed.ps1 não encontrado no runtime.")
		return

	DirAccess.make_dir_recursive_absolute(Paths.temp_dir)
	_status_file = Paths.temp_dir.path_join("setup_status.json")
	if FileAccess.file_exists(_status_file):
		DirAccess.remove_absolute(_status_file)

	var args := [
		"-ExecutionPolicy", "Bypass",
		"-NoProfile",
		"-File", script,
		"-StatusFile", _status_file,
	]
	if aio:
		args.append("-Aio")

	_pid = OS.create_process("powershell.exe", args)
	if _pid == -1:
		failed.emit("Não consegui iniciar o powershell.exe.")
		return

	_running = true
	_last_message = ""
	progress.emit(0, "Iniciando instalação...")
	_timer.start()


func _poll() -> void:
	var data := StatusContract.read(_status_file)

	if not data.is_empty():
		var status: String = data.get("status", "")
		var message: String = str(data.get("message", ""))
		var percent: int = int(data.get("progress", 0))
		if message != "":
			_last_message = message
		match status:
			"done":
				_stop()
				Paths.refresh()   # o embeddable passou a existir; reaponta os caminhos
				finished.emit()
				return
			"error":
				_stop()
				failed.emit(message)
				return
			_:
				progress.emit(percent, message)

	# Se o processo morreu sem ter reportado 'done'/'error', é falha — mostra a
	# última mensagem conhecida (evita ficar preso em 0% quando o script quebra).
	if _pid != -1 and not OS.is_process_running(_pid):
		_stop()
		var reason := _last_message if _last_message != "" else "O instalador encerrou inesperadamente."
		failed.emit(reason)


func _stop() -> void:
	_timer.stop()
	_running = false
	_pid = -1
