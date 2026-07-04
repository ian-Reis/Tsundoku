extends Panel

## Card de um item da fila de download. Mostra o nome do livro e o progresso.
## É instanciado pelo execute.gd e adicionado no QueueVBoxContainer.

## Emitido quando o usuário clica no botão X deste card. O execute.gd decide o
## que fazer (tirar da fila ou matar o processo), pois só ele conhece a fila.
signal cancel_requested(task: Node)

@onready var name_label: Label = %NameLabel
@onready var progress_bar: ProgressBar = %ProgressBar
@onready var cancel_button: Button = %CancelButton

# Título "puro" (sem o sufixo de estado). Guardado à parte para poder
# recompor "Título  •  estado" sem perder o nome do livro.
var _title: String = ""


func _ready() -> void:
	if _title == "":
		name_label.text = ""
	progress_bar.value = 0
	cancel_button.pressed.connect(func(): cancel_requested.emit(self))


## Desabilita o botão de cancelar (usado quando o job terminou/falhou e não faz
## mais sentido cancelar).
func disable_cancel() -> void:
	if is_node_ready():
		cancel_button.disabled = true


## Define o nome inicial do card (antes do título real chegar do Python,
## costuma ser a URL encurtada).
func setup(display_name: String) -> void:
	_title = display_name
	if is_node_ready():
		name_label.text = display_name
		progress_bar.value = 0


## Atualiza o título quando o Python reporta o nome real da série (status
## "preparing"). Preserva o sufixo de estado atual, se houver.
func set_title(title: String) -> void:
	_title = title
	if is_node_ready():
		name_label.text = title


func set_progress(value: float) -> void:
	if is_node_ready():
		progress_bar.value = value


## Mostra o estado atual ("na fila", "baixando cap 3/40", "concluído", ...)
## como sufixo do título, e tinge o texto conforme a cor passada.
func set_state(text: String, color: Color = Color.WHITE) -> void:
	if not is_node_ready():
		return
	if text == "":
		name_label.text = _title
	else:
		name_label.text = "%s  •  %s" % [_title, text]
	name_label.modulate = color
