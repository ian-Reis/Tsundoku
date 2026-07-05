extends Node

## Autoload "ProcessManager" — o motor de download, sem UI. Dono da fila (um job
## por vez), do spawn de subprocesso (create_process, não bloqueia), do status
## file por job (status_<id>.json, sem sobrescrita entre jobs), do polling e do
## cancelamento. Telas apenas chamam submit()/cancel() e escutam os sinais.

signal job_started(job: Job)
signal job_progress(job: Job, data: Dictionary)   # starting/preparing/downloading/cooldown
signal job_finished(job: Job, data: Dictionary)   # done
signal job_failed(job: Job, message: String)      # error / crash / falha no spawn
signal job_cancelled(job: Job)                     # cancelado pelo usuário ou pelo script

var _queue: Array[Job] = []
var _current: Job = null
var _next_id: int = 0
var _poll_timer: Timer


func _ready() -> void:
	_poll_timer = Timer.new()
	_poll_timer.wait_time = 0.3
	_poll_timer.timeout.connect(_poll)
	add_child(_poll_timer)


func submit(job: Job) -> void:
	job.id = _next_id
	_next_id += 1
	job.status_file = Paths.temp_dir.path_join("status_%d.json" % job.id)
	_queue.append(job)
	# Inicia no próximo idle, não agora: dá tempo da view criar o card (com o id
	# já atribuído acima) antes do job_started ser emitido.
	_try_start_next.call_deferred()


func cancel(job: Job) -> void:
	# Rodando agora: mata o processo e encerra.
	if _current == job:
		if job.pid != -1 and OS.is_process_running(job.pid):
			OS.kill(job.pid)
		job_cancelled.emit(job)
		_finish_current()
		return
	# Só na fila: remove sem ter iniciado.
	var idx := _queue.find(job)
	if idx != -1:
		_queue.remove_at(idx)
		job_cancelled.emit(job)


func is_busy() -> bool:
	return _current != null


func queue_size() -> int:
	return _queue.size()


func _try_start_next() -> void:
	if _current != null:
		return
	if _queue.is_empty():
		return
	_current = _queue.pop_front()
	_start(_current)


func _start(job: Job) -> void:
	# Garante temp/ e limpa status antigo desse id (senão o 1º poll lê rodada anterior).
	DirAccess.make_dir_recursive_absolute(Paths.temp_dir)
	if FileAccess.file_exists(job.status_file):
		DirAccess.remove_absolute(job.status_file)

	# Higiene de encoding no Windows (a saga do cp1252).
	OS.set_environment("PYTHONUTF8", "1")

	job.pid = OS.create_process(Paths.python_exe, job.build_args())
	if job.pid == -1:
		push_error("create_process falhou: %s" % Paths.python_exe)
		# Código de razão (não texto): a view localiza. Ver _on_job_failed.
		job_failed.emit(job, "err:spawn")
		_current = null
		_try_start_next()
		return

	job_started.emit(job)
	_poll_timer.start()


func _poll() -> void:
	if _current == null:
		_poll_timer.stop()
		return

	var data := StatusContract.read(_current.status_file)
	if data.is_empty():
		# Sem arquivo ainda. Se o processo já morreu sem escrever, foi crash.
		if not FileAccess.file_exists(_current.status_file) \
				and not OS.is_process_running(_current.pid):
			var job := _current
			push_error("Processo Python encerrou sem escrever status.")
			job_failed.emit(job, "err:no_status")
			_finish_current()
		return

	var status: String = data.get(StatusContract.F_STATUS, "")
	match status:
		StatusContract.DONE:
			var job := _current
			job_finished.emit(job, data)
			_finish_current()
		StatusContract.ERROR:
			var job := _current
			job_failed.emit(job, str(data.get(StatusContract.F_MESSAGE, "?")))
			_finish_current()
		StatusContract.CANCELLED:
			var job := _current
			job_cancelled.emit(job)
			_finish_current()
		_:
			job_progress.emit(_current, data)


func _finish_current() -> void:
	_poll_timer.stop()
	_current = null
	_try_start_next()
