class_name Sources
extends RefCounted

## Registro das fontes de download. Cada fonte é um DADO, não um ramo de código:
## adicionar uma fonte nova = acrescentar um bloco em DEFS, sem tocar na fila,
## no spawn de processo ou no parsing de status.
##
## Campos de cada def:
##   id        : índice no OptionButton (e identidade da fonte)
##   key       : chave I18n da palavra do tipo ("type_novel" / "type_manga")
##   site      : nome próprio do site (não traduzido)
##   script    : arquivo Python na raiz do runtime
##   folder    : subpasta de saída em downloads/<folder>/
##   volumes   : aceita --volumes?
##   lang_flag : flag de idioma de conteúdo ("" = não tem; "--lang"/"--language")
##   aio       : precisa de --aio-python (roda o AIO via wrapper)?

const NOVEL := 0
const MANGALIVRE := 1
const MANGAFIRE := 2
const MANGADEX := 3

const DEFS := [
	{
		id = NOVEL, key = "type_novel", site = "CentralNovel", folder = "centralnovel",
		script = "centralnovel_dlv7.py", volumes = true, lang_flag = "",
	},
	{
		id = MANGALIVRE, key = "type_manga", site = "MangaLivre", folder = "mangalivre",
		script = "mangalivre_dlv4.py", volumes = false, lang_flag = "",
	},
	{
		id = MANGAFIRE, key = "type_manga", site = "MangaFire", folder = "mangafire",
		script = "aio_dl_wrapper.py", volumes = false, lang_flag = "--language",
		aio = true,
	},
	{
		id = MANGADEX, key = "type_manga", site = "MangaDex", folder = "mangadex",
		script = "mangadex_dl.py", volumes = false, lang_flag = "--lang",
	},
]


static func get_def(id: int) -> Dictionary:
	for d in DEFS:
		if d.id == id:
			return d
	return {}


## Rótulo pronto pra UI: "Tipo (Site)" com o tipo traduzido.
static func label(id: int) -> String:
	var d := get_def(id)
	if d.is_empty():
		return ""
	return "%s (%s)" % [I18n.t(d.key), d.site]
