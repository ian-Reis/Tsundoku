extends Node

## Singleton de internacionalização (autoload "I18n").
##
## Guarda os textos da UI em PT-BR e English num dicionário e serve por chave via
## t(). Não usa o TranslationServer do Godot de propósito: como boa parte das
## strings é montada em código (status, estados dos cards, com formatação %),
## um dicionário próprio + sinal de troca é mais simples e determinístico.
##
## A preferência é salva em user://settings.cfg e recarregada no boot.

signal language_changed(locale: String)

const CONFIG_PATH := "user://settings.cfg"
const DEFAULT_LOCALE := "pt_BR"

# Locales suportados, na ordem que aparecem nos seletores.
const LOCALES := ["pt_BR", "en"]

var current_locale: String = DEFAULT_LOCALE

var _strings := {
	"pt_BR": {
		# UI estática
		"app_download": "Baixar",
		"ph_url": "Url - https://manga...",
		"ph_volumes": "volumes ex:1,1-5",
		"ph_chapters": "chapters ex:1,1-5",
		# Só a palavra do tipo é traduzida; o nome do site é composto em código
		# ("%s (MangaFire)"), pra não repetir nomes próprios por idioma.
		"type_novel": "Novel",
		"type_manga": "Mangá",
		# mensagens de status geral
		"status_intro": "Cole uma URL e clique em Baixar.",
		"status_need_url": "Cole uma URL primeiro.",
		"status_manga_unavailable": "Download de mangá ainda não implementado.",
		"status_aio_missing": "Wrapper do AIO não encontrado (aio_dl_wrapper.py).",
		"status_mangadex_missing": "Script do MangaDex não encontrado (mangadex_dl.py).",
		"status_in_queue": "%d na fila.",
		"status_queue_empty": "Fila vazia.",
		"status_downloading_n": "Baixando 1 de %d...",
		"status_removed": "Removido da fila.",
		# estados dos cards
		"st_queued": "na fila",
		"st_starting": "iniciando...",
		"st_preparing": "preparando...",
		"st_chapters": "%d capítulos",
		"st_chapter_of": "cap %d/%d",
		"st_cooldown": "cooldown %ds...",
		"st_done": "concluído (%d cap)",
		"st_error": "erro: %s",
		"st_cancelled": "cancelado",
		"st_no_start": "falha ao iniciar",
		"st_no_status": "encerrou sem status",
	},
	"en": {
		"app_download": "Download",
		"ph_url": "URL - https://manga...",
		"ph_volumes": "volumes eg:1,1-5",
		"ph_chapters": "chapters eg:1,1-5",
		"type_novel": "Novel",
		"type_manga": "Manga",
		"status_intro": "Paste a URL and click Download.",
		"status_need_url": "Paste a URL first.",
		"status_manga_unavailable": "Manga download not implemented yet.",
		"status_aio_missing": "AIO wrapper not found (aio_dl_wrapper.py).",
		"status_mangadex_missing": "MangaDex script not found (mangadex_dl.py).",
		"status_in_queue": "%d in queue.",
		"status_queue_empty": "Queue empty.",
		"status_downloading_n": "Downloading 1 of %d...",
		"status_removed": "Removed from queue.",
		"st_queued": "queued",
		"st_starting": "starting...",
		"st_preparing": "preparing...",
		"st_chapters": "%d chapters",
		"st_chapter_of": "ch %d/%d",
		"st_cooldown": "cooldown %ds...",
		"st_done": "done (%d ch)",
		"st_error": "error: %s",
		"st_cancelled": "cancelled",
		"st_no_start": "failed to start",
		"st_no_status": "ended without status",
	},
}


func _ready() -> void:
	_load_saved_locale()


## Traduz uma chave. Se args não estiver vazio, aplica formatação (%).
## Ex: I18n.t("status_in_queue", [3])  ->  "3 na fila."
func t(key: String, args: Array = []) -> String:
	var table: Dictionary = _strings.get(current_locale, _strings[DEFAULT_LOCALE])
	var text: String = table.get(key, key)
	if args.is_empty():
		return text
	return text % args


func set_locale(locale: String) -> void:
	if locale == current_locale or not _strings.has(locale):
		return
	current_locale = locale
	_save_locale()
	language_changed.emit(locale)


## Índice do locale atual dentro de LOCALES (útil pra setar o OptionButton).
func locale_index() -> int:
	return maxi(0, LOCALES.find(current_locale))


func _load_saved_locale() -> void:
	var cfg := ConfigFile.new()
	if cfg.load(CONFIG_PATH) == OK:
		var saved: String = cfg.get_value("app", "locale", DEFAULT_LOCALE)
		if _strings.has(saved):
			current_locale = saved


func _save_locale() -> void:
	var cfg := ConfigFile.new()
	cfg.load(CONFIG_PATH)  # ignora erro: se não existe, começa vazio
	cfg.set_value("app", "locale", current_locale)
	cfg.save(CONFIG_PATH)
