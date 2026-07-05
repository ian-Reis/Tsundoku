class_name Library
extends RefCounted

## Varre a pasta downloads/<fonte>/<título>/ e lista o que já foi baixado.
## Cada título vira um dicionário com seus "capítulos" (arquivos .cbz/.pdf/.epub).
## Lógica pura, sem UI — a tela de Biblioteca consome isto.

# Formatos que o leitor in-app abre (CBZ = zip de imagens).
const CBZ_EXTS := ["cbz", "zip"]
# Formatos que delegamos ao app do sistema (OS.shell_open).
const OPEN_EXTS := ["pdf", "epub"]


## Retorna Array de títulos:
##   { source, title, path, chapters: [ { name, path, ext }, ... ] }
static func scan() -> Array:
	var result: Array = []
	var root := Paths.output_dir
	if not DirAccess.dir_exists_absolute(root):
		return result

	for source in DirAccess.get_directories_at(root):
		var source_dir := root.path_join(source)
		for title in DirAccess.get_directories_at(source_dir):
			var title_dir := source_dir.path_join(title)
			var chapters := _list_readable(title_dir)
			if chapters.is_empty():
				continue
			result.append({
				"source": source,
				"title": title,
				"path": title_dir,
				"chapters": chapters,
			})

	result.sort_custom(func(a, b): return a.title.naturalnocasecmp_to(b.title) < 0)
	return result


## True se a extensão é lida dentro do app (CBZ).
static func is_readable_in_app(ext: String) -> bool:
	return ext.to_lower() in CBZ_EXTS


const _IMAGE_EXTS := ["jpg", "jpeg", "png", "webp", "bmp"]
const COVER_WIDTH := 160   # largura da miniatura (altura proporcional)


## Capa do título: a 1ª página do 1º capítulo CBZ, já reduzida a miniatura.
## Retorna null se o título só tem PDF/EPUB (sem imagem pra extrair).
static func cover_for(item: Dictionary) -> Texture2D:
	for ch in item.chapters:
		if ch.ext in CBZ_EXTS:
			return _cbz_first_image(ch.path)
	return null


static func _cbz_first_image(path: String) -> Texture2D:
	var zip := ZIPReader.new()
	if zip.open(path) != OK:
		return null

	var pages: Array[String] = []
	for entry in zip.get_files():
		if entry.get_extension().to_lower() in _IMAGE_EXTS:
			pages.append(entry)
	if pages.is_empty():
		zip.close()
		return null
	pages.sort_custom(func(a, b): return a.naturalnocasecmp_to(b) < 0)

	var data := zip.read_file(pages[0])
	zip.close()

	var img := Image.new()
	var err: int
	match pages[0].get_extension().to_lower():
		"png": err = img.load_png_from_buffer(data)
		"webp": err = img.load_webp_from_buffer(data)
		"bmp": err = img.load_bmp_from_buffer(data)
		_: err = img.load_jpg_from_buffer(data)
	if err != OK:
		return null

	# Reduz pra miniatura (economiza memória — não guardamos a página cheia).
	if img.get_width() > COVER_WIDTH:
		var h := int(img.get_height() * float(COVER_WIDTH) / img.get_width())
		img.resize(COVER_WIDTH, maxi(1, h), Image.INTERPOLATE_BILINEAR)
	return ImageTexture.create_from_image(img)


static func _list_readable(dir: String) -> Array:
	var out: Array = []
	for f in DirAccess.get_files_at(dir):
		var ext := f.get_extension().to_lower()
		if ext in CBZ_EXTS or ext in OPEN_EXTS:
			out.append({"name": f, "path": dir.path_join(f), "ext": ext})
	out.sort_custom(func(a, b): return a.name.naturalnocasecmp_to(b.name) < 0)
	return out
