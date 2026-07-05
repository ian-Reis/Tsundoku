extends HBoxContainer

## Um item da Biblioteca: capa + título + botões de capítulo. Instanciado pela
## library.gd. Emite chapter_selected quando o usuário clica num capítulo — a
## tela decide o que fazer (abrir no leitor ou no app do sistema).

signal chapter_selected(chapter: Dictionary)

@onready var cover: TextureRect = %Cover
@onready var title_label: Label = %TitleLabel
@onready var chapters_flow: HFlowContainer = %ChaptersFlow


## Popula o card a partir de um título vindo de Library.scan().
func setup(item: Dictionary) -> void:
	title_label.text = "%s   ·   %s   (%d)" % [item.title, item.source, item.chapters.size()]

	var tex := Library.cover_for(item)
	if tex != null:
		cover.texture = tex

	for ch in item.chapters:
		var btn := Button.new()
		btn.text = ch.name
		btn.clip_text = true
		btn.custom_minimum_size = Vector2(220, 0)
		btn.pressed.connect(func(): chapter_selected.emit(ch))
		chapters_flow.add_child(btn)
