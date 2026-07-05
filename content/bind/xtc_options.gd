extends VBoxContainer

## Painel de opções de exportação XTC (cbz2xtc). build_args() traduz cada controle
## nos flags do cbz2xtc. Embutido na tela de Juntar, visível só quando o formato
## XTC está selecionado.

const DITHERS := ["stucki", "atkinson", "ostromoukhov", "zhoufang", "stochastic", "floyd", "ordered", "rasterize", "none"]
const DOWNSCALES := ["bicubic", "bilinear", "box", "lanczos", "nearest"]
const LANDSCAPES := ["none", "ltr", "rtl"]
# Flag por item do OverviewOption (Nenhum / Portrait / Girado). Nomes enganosos
# no cbz2xtc: --sideways-overviews = PORTRAIT/em pé.
const OVERVIEW_FLAGS := ["", "--sideways-overviews", "--include-overviews"]

@onready var dither: OptionButton = %DitherOption
@onready var downscale: OptionButton = %DownscaleOption
@onready var landscape: OptionButton = %LandscapeOption
@onready var overview: OptionButton = %OverviewOption
@onready var gamma: SpinBox = %GammaSpin
@onready var contrast: SpinBox = %ContrastSpin
@onready var manhwa_overlap: SpinBox = %ManhwaOverlapSpin
@onready var two_bit: CheckBox = %TwoBitCheck
@onready var compress: CheckBox = %CompressCheck
@onready var invert: CheckBox = %InvertCheck
@onready var manhwa: CheckBox = %ManhwaCheck
@onready var keep_cover: CheckBox = %KeepCoverCheck
@onready var lbl_gamma: Label = %LblGamma
@onready var lbl_contrast: Label = %LblContrast
@onready var lbl_overview: Label = %LblOverview
@onready var split_all: CheckBox = %SplitAllCheck
@onready var pad_black: CheckBox = %PadBlackCheck
@onready var overlap: CheckBox = %OverlapCheck


func _ready() -> void:
	for d in DITHERS:
		dither.add_item(d)
	for d in DOWNSCALES:
		downscale.add_item(d)
	for l in LANDSCAPES:
		landscape.add_item(l)
	overview.add_item("")
	overview.add_item("")
	overview.add_item("")
	# Defaults do xtcjs.app: dithering Floyd-Steinberg, overview em portrait.
	dither.selected = maxi(0, DITHERS.find("floyd"))
	downscale.selected = 0
	landscape.selected = 0
	overview.selected = 1   # Portrait (em pé)

	_apply_language()


## Textos localizados (a tela é recriada a cada abertura, então setar no _ready
## já pega o idioma atual). Só os rótulos descritivos; nomes técnicos (algoritmos
## de dither/downscale) ficam como estão.
func _apply_language() -> void:
	lbl_gamma.text = I18n.t("xopt_gamma")
	lbl_contrast.text = I18n.t("xopt_contrast")
	lbl_overview.text = I18n.t("xopt_overview")
	overview.set_item_text(0, I18n.t("xopt_ov_none"))
	overview.set_item_text(1, I18n.t("xopt_ov_portrait"))
	overview.set_item_text(2, I18n.t("xopt_ov_sideways"))
	invert.text = I18n.t("xopt_invert")
	pad_black.text = I18n.t("xopt_pad_black")
	keep_cover.text = I18n.t("xopt_keep_cover")
	manhwa.text = I18n.t("xopt_manhwa")


## Monta os argumentos do cbz2xtc a partir dos controles.
func build_args() -> Array:
	var a: Array = ["--clean"]   # apaga PNGs temporários ao final

	var dith: String = DITHERS[dither.selected]
	if dith == "none":
		a.append("--no-dither")
	else:
		a += ["--dither", dith]

	a += ["--downscale", DOWNSCALES[downscale.selected]]

	if two_bit.button_pressed:
		a.append("--2bit")
	if compress.button_pressed:
		a.append("--compress")
	if invert.button_pressed:
		a.append("--invert")

	if absf(gamma.value - 1.0) > 0.001:
		a += ["--gamma", str(gamma.value)]
	if contrast.value > 0:
		a += ["--contrast-boost", str(int(contrast.value))]

	if manhwa.button_pressed:
		a += ["--manhwa", str(int(manhwa_overlap.value))]

	var land: String = LANDSCAPES[landscape.selected]
	if land != "none":
		a += ["--landscape-page-split", land]

	# Overview da página antes dos cortes: Nenhum / Portrait / Girado. O flag é
	# escolhido pela tabela OVERVIEW_FLAGS. A capa fica portrait de qualquer
	# jeito (patch no cbz2xtc), independente disto.
	var ov_flag: String = OVERVIEW_FLAGS[overview.selected]
	if ov_flag != "":
		a.append(ov_flag)
	if keep_cover.button_pressed:
		# Mantém a capa (página 1) inteira, sem cortar em pedaços.
		a += ["--dont-split", "1"]
	if split_all.button_pressed:
		a.append("--split-all")
	if pad_black.button_pressed:
		a.append("--pad-black")
	if overlap.button_pressed:
		a.append("--overlap")

	return a
