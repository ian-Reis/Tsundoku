# setup_embed.ps1 — monta um Python EMBEDDABLE autocontido, com pip + deps, sem
# depender de Python instalado na máquina. Pode ser chamado pelo próprio app
# (via powershell.exe, que existe em todo Windows) ou à mão:
#
#   powershell -ExecutionPolicy Bypass -File setup_embed.ps1
#   powershell -ExecutionPolicy Bypass -File setup_embed.ps1 -Aio
#
# -Aio      : monta o embeddable do AIO em runtime/aio/python/ (deps pesadas).
# -StatusFile: se dado, escreve progresso em JSON pro app fazer polling.
#
# O Paths.gd detecta runtime/python/ (e runtime/aio/python/) automaticamente.

param(
    [string]$PyVersion = "3.12.7",
    [switch]$Aio,
    [string]$StatusFile = ""
)

$ErrorActionPreference = "Stop"

# Base: runtime/ para o modo enxuto, runtime/aio/ para o AIO. Sempre derivado da
# localização do script ($PSScriptRoot = runtime/), não do diretório atual.
$scriptRoot = $PSScriptRoot
$base = if ($Aio) { Join-Path $scriptRoot "aio" } else { $scriptRoot }
$pyDir = Join-Path $base "python"

function Write-Status($status, $message, $progress) {
    if (-not $StatusFile) { return }
    $obj = @{ status = $status; message = $message; progress = $progress } | ConvertTo-Json -Compress
    $tmp = "$StatusFile.tmp"
    # UTF-8 SEM BOM: o Windows PowerShell coloca BOM por padrão e o JSON.parse do
    # Godot engasga com ele. WriteAllText com UTF8Encoding($false) evita o BOM.
    [System.IO.File]::WriteAllText($tmp, $obj, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -Force $tmp $StatusFile
}

try {
    Write-Status "running" "Baixando Python $PyVersion..." 0

    # $env:TEMP costuma vir no formato curto 8.3 (ex.: C:\Users\TUFF16~1\...) quando
    # o nome do usuário tem espaço/é longo, e o Remote-Item do PowerShell quebra
    # nesse "~". Resolve pro caminho longo pra evitar isso.
    $tmpDir = (Get-Item $env:TEMP).FullName

    $zipUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
    $zipPath = Join-Path $tmpDir "python-embed-$PyVersion.zip"

    if (Test-Path $pyDir) { Remove-Item -Recurse -Force $pyDir }
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $pyDir -Force
    Remove-Item $zipPath
    Write-Status "running" "Python extraído. Habilitando pacotes..." 25

    # Destrava o embeddable: habilita "import site" e adiciona Lib\site-packages,
    # senão pip e as deps não seriam carregados.
    $pth = Get-ChildItem -Path $pyDir -Filter "python*._pth" | Select-Object -First 1
    $lines = Get-Content $pth.FullName | ForEach-Object { $_ -replace '^\s*#\s*import site', 'import site' }
    if ($lines -notcontains 'Lib\site-packages') { $lines += 'Lib\site-packages' }
    Set-Content -Path $pth.FullName -Value $lines -Encoding ascii

    $py = Join-Path $pyDir "python.exe"

    Write-Status "running" "Instalando pip..." 40
    $getPip = Join-Path $tmpDir "get-pip.py"
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
    & $py $getPip --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "get-pip falhou" }
    Remove-Item $getPip

    Write-Status "running" "Instalando dependências..." 60
    & $py -m pip install --no-warn-script-location -r (Join-Path $base "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install falhou" }

    if (-not $Aio) {
        Write-Status "running" "Baixando navegador (Chromium)..." 80
        $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $scriptRoot "pw-browsers"
        & $py -m playwright install chromium
        if ($LASTEXITCODE -ne 0) { throw "playwright install falhou" }
    }

    Write-Status "done" "Runtime instalado." 100
    exit 0
}
catch {
    $line = $_.InvocationInfo.ScriptLineNumber
    Write-Status "error" "linha ${line}: $($_.Exception.Message)" 0
    Write-Error $_
    exit 1
}
