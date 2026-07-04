extends Control

@onready var url_text_edit: TextEdit = %UrlTextEdit
@onready var download_button: Button = %DownloadButton
@onready var progress_bar: ProgressBar = %ProgressBar   # adicione no .tscn
@onready var status_label: Label = %StatusLabel         # adicione no .tscn

# Caminhos do runtime. globalize_path converte res:// pra um caminho absoluto
# do sistema — necessário porque o processo Python roda fora do Godot e não
# entende res://.
var runtime_dir: String = ProjectSettings.globalize_path("res://content/runtime")
var python_exe: String = runtime_dir.path_join(".venv/Scripts/python.exe")
var script_path: String = runtime_dir.path_join("centralnovel_dlv7.py")
var status_file: String = runtime_dir.path_join("temp/status.json")
var output_dir: String = runtime_dir.path_join("downloads")

var current_pid: int = -1
var poll_timer: Timer


func _ready() -> void:
	download_button.pressed.connect(on_download_pressed)

	# Timer que faz polling do status.json enquanto o download roda.
	poll_timer = Timer.new()
	poll_timer.wait_time = 0.3
	poll_timer.timeout.connect(_poll_status)
	add_child(poll_timer)


func on_download_pressed() -> void:
	var url := url_text_edit.text.strip_edges()
	if url == "":
		status_label.text = "Cole uma URL primeiro."
		return

	# Evita disparar dois downloads ao mesmo tempo (por enquanto; a fila vem depois).
	if current_pid != -1 and OS.is_process_running(current_pid):
		status_label.text = "Já existe um download em andamento."
		return

	# Garante que a pasta temp existe e limpa status antigo, senão o primeiro
	# poll pode ler o resultado de um download anterior.
	DirAccess.make_dir_recursive_absolute(status_file.get_base_dir())
	if FileAccess.file_exists(status_file):
		DirAccess.remove_absolute(status_file)

	# Higiene de encoding no Windows (a saga do cp1252). Não custa nada e
	# evita que erros futuros venham mascarados.
	OS.set_environment("PYTHONUTF8", "1")

	var args := [
		script_path,
		url,
		"--chapters", "1",
		"--output", output_dir,
		"--status-file", status_file,
	]

	# create_process NÃO bloqueia — retorna o PID e o Python roda em paralelo.
	current_pid = OS.create_process(python_exe, args)

	if current_pid == -1:
		status_label.text = "Falha ao iniciar o Python. Confira o caminho da venv."
		push_error("create_process falhou: %s" % python_exe)
		return

	download_button.disabled = true
	progress_bar.value = 0
	status_label.text = "Iniciando..."
	poll_timer.start()


func _poll_status() -> void:
	if not FileAccess.file_exists(status_file):
		# Processo pode ter começado mas ainda não escreveu o primeiro status.
		# Se o processo já morreu sem gerar arquivo, algo deu errado.
		if current_pid != -1 and not OS.is_process_running(current_pid):
			_on_process_gone_without_status()
		return

	var file := FileAccess.open(status_file, FileAccess.READ)
	if file == null:
		return  # arquivo sendo reescrito nesse instante; tenta no próximo tick

	var content := file.get_as_text()
	file.close()

	var json := JSON.new()
	if json.parse(content) != OK:
		return  # JSON incompleto por race rara; ignora e tenta de novo

	var data: Dictionary = json.data
	_apply_status(data)


func _apply_status(data: Dictionary) -> void:
	var status: String = data.get("status", "")

	match status:
		"starting", "preparing":
			status_label.text = "Preparando..."
			if data.has("total_chapters"):
				status_label.text = "Encontrados %d capítulos." % data["total_chapters"]

		"downloading":
			progress_bar.value = data.get("progress", 0)
			status_label.text = "Baixando cap %d de %d" % [
				data.get("current_chapter", 0),
				data.get("total_chapters", 0),
			]

		"cooldown":
			# Estado precioso: mostra que está esperando, não travado.
			status_label.text = "Cooldown do site — aguardando %ds..." % data.get("wait_seconds", 0)

		"done":
			progress_bar.value = 100
			status_label.text = "Concluído! %d capítulos em %s" % [
				data.get("done", 0),
				data.get("output_path", ""),
			]
			_finish_download()

		"error":
			status_label.text = "Erro: %s" % data.get("message", "desconhecido")
			_finish_download()

		"cancelled":
			status_label.text = "Cancelado."
			_finish_download()


func _finish_download() -> void:
	poll_timer.stop()
	download_button.disabled = false
	current_pid = -1


func _on_process_gone_without_status() -> void:
	# O Python morreu sem escrever nem um status — provavelmente crash na
	# inicialização (venv errada, import faltando, etc.).
	poll_timer.stop()
	download_button.disabled = false
	current_pid = -1
	status_label.text = "O processo encerrou sem gerar status. Rode o script no terminal pra ver o erro."
	push_error("Processo Python encerrou sem escrever status.json")
