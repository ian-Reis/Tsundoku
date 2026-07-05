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


static func _list_readable(dir: String) -> Array:
	var out: Array = []
	for f in DirAccess.get_files_at(dir):
		var ext := f.get_extension().to_lower()
		if ext in CBZ_EXTS or ext in OPEN_EXTS:
			out.append({"name": f, "path": dir.path_join(f), "ext": ext})
	out.sort_custom(func(a, b): return a.name.naturalnocasecmp_to(b.name) < 0)
	return out
