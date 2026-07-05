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
		"type_other": "Outros sites",
		# mensagens de status geral
		"status_intro": "Cole uma URL e clique em Baixar.",
		"status_need_url": "Cole uma URL primeiro.",
		"status_source_missing": "Downloader não encontrado: %s",
		"status_in_queue": "%d na fila.",
		"status_queue_empty": "Fila vazia.",
		"status_downloading_n": "Baixando 1 de %d...",
		"status_removed": "Removido da fila.",
		# instalador do runtime
		"app_install": "Instalar runtime",
		"setup_needed": "Runtime Python não instalado. Clique em Instalar runtime.",
		"setup_progress": "%s (%d%%)",
		"setup_done": "Runtime instalado com sucesso.",
		"setup_failed": "Falha na instalação: %s",
		"app_open_downloads": "Abrir pasta",
		"status_open_downloads_failed": "Não consegui abrir a pasta de downloads.",
		"app_library": "Biblioteca",
		"app_refresh": "Atualizar",
		"lib_empty": "Nada baixado ainda.",
		"app_bind": "Exportar",
		"bind_title": "Exportar",
		"bind_lbl_source": "Título baixado:",
		"bind_lbl_format": "Formato:",
		"bind_lbl_name": "Nome do arquivo:",
		"bind_working": "Juntando...",
		"xtc_working": "Convertendo pro e-reader...",
		# rótulos do painel de opções XTC
		"xopt_gamma": "Gamma (brilho)",
		"xopt_contrast": "Contraste (0-8)",
		"xopt_overview": "Overview da página",
		"xopt_ov_none": "Nenhum",
		"xopt_ov_portrait": "Portrait (em pé)",
		"xopt_ov_sideways": "Girado",
		"xopt_invert": "Inverter",
		"xopt_pad_black": "Pad preto",
		"xopt_keep_cover": "Manter capa inteira",
		"xopt_manhwa": "Manhwa (webtoon)",
		"bind_done": "Pronto: %s",
		"bind_error": "Erro: %s",
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
		"type_other": "Other sites",
		"status_intro": "Paste a URL and click Download.",
		"status_need_url": "Paste a URL first.",
		"status_source_missing": "Downloader not found: %s",
		"status_in_queue": "%d in queue.",
		"status_queue_empty": "Queue empty.",
		"status_downloading_n": "Downloading 1 of %d...",
		"status_removed": "Removed from queue.",
		"app_install": "Install runtime",
		"setup_needed": "Python runtime not installed. Click Install runtime.",
		"setup_progress": "%s (%d%%)",
		"setup_done": "Runtime installed successfully.",
		"setup_failed": "Install failed: %s",
		"app_open_downloads": "Open folder",
		"status_open_downloads_failed": "Couldn't open the downloads folder.",
		"app_library": "Library",
		"app_refresh": "Refresh",
		"lib_empty": "Nothing downloaded yet.",
		"app_bind": "Export",
		"bind_title": "Export",
		"bind_lbl_source": "Downloaded title:",
		"bind_lbl_format": "Format:",
		"bind_lbl_name": "File name:",
		"bind_working": "Binding...",
		"xtc_working": "Converting for e-reader...",
		"xopt_gamma": "Gamma (brightness)",
		"xopt_contrast": "Contrast (0-8)",
		"xopt_overview": "Page overview",
		"xopt_ov_none": "None",
		"xopt_ov_portrait": "Portrait (upright)",
		"xopt_ov_sideways": "Sideways",
		"xopt_invert": "Invert",
		"xopt_pad_black": "Pad black",
		"xopt_keep_cover": "Keep cover whole",
		"xopt_manhwa": "Manhwa (webtoon)",
		"bind_done": "Done: %s",
		"bind_error": "Error: %s",
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
