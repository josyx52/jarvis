# =============================================================
#  Jarvis — Publicar nova versao do agente
#
#  Uso:
#    .\publish_version.ps1 -Version 0.5.0
#
#  O script:
#    1. Copia o novo installer para jarvis_center\installers\
#    2. Actualiza JARVIS_AGENT_VERSION e JARVIS_INSTALLER_PATH no .env
#    3. Reinicia o center (se estiver a correr como servico)
#
#  Os agents actualizam-se automaticamente na proxima verificacao (ate 1h).
# =============================================================

param(
    [Parameter(Mandatory=$true)]
    [string]$Version
)

$ErrorActionPreference = "Stop"

# --- Caminhos ---
$CenterDir   = $PSScriptRoot
$BuildOutput = "$CenterDir\..\release\installer\output\JarvisAgent_Setup_$Version.exe"
$InstallDir  = "$CenterDir\installers"
$EnvFile     = "$CenterDir\.env"
$Dest        = "$InstallDir\JarvisAgent_Setup_$Version.exe"

Write-Host ""
Write-Host "============================================"
Write-Host " Jarvis — Publicar versao $Version"
Write-Host "============================================"
Write-Host ""

# 1. Verificar que o installer foi compilado
if (-not (Test-Path $BuildOutput)) {
    Write-Error "Installer nao encontrado: $BuildOutput`nCorre primeiro o build (build.bat ou PyInstaller + ISCC)."
}

# 2. Copiar para a pasta installers/
Write-Host "[1/3] A copiar installer para installers\..."
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item $BuildOutput $Dest -Force
$sizeMb = [math]::Round((Get-Item $Dest).Length / 1MB, 1)
Write-Host "      OK — $([System.IO.Path]::GetFileName($Dest)) ($sizeMb MB)"

# 3. Actualizar o .env
Write-Host "[2/3] A actualizar .env..."

$envContent = Get-Content $EnvFile -Raw

# Remover linhas existentes (se houver)
$envContent = $envContent -replace "(?m)^JARVIS_AGENT_VERSION=.*$(\r?\n)?", ""
$envContent = $envContent -replace "(?m)^JARVIS_INSTALLER_PATH=.*$(\r?\n)?", ""
$envContent = $envContent.TrimEnd()

# Adicionar as novas linhas
$AbsoluteDest = (Resolve-Path $Dest).Path
$envContent += "`r`n`r`n# Auto-update do agente`r`nJARVIS_AGENT_VERSION=$Version`r`nJARVIS_INSTALLER_PATH=$AbsoluteDest`r`n"

Set-Content $EnvFile $envContent -Encoding UTF8 -NoNewline
Write-Host "      OK — JARVIS_AGENT_VERSION=$Version"
Write-Host "      OK — JARVIS_INSTALLER_PATH=$AbsoluteDest"

# 4. Reiniciar o center
Write-Host "[3/3] A reiniciar o Jarvis Center..."
$svc = Get-Service -Name "JarvisCenter" -ErrorAction SilentlyContinue
if ($svc) {
    Restart-Service "JarvisCenter" -Force
    Write-Host "      OK — servico JarvisCenter reiniciado."
} else {
    Write-Host "      AVISO — servico JarvisCenter nao encontrado."
    Write-Host "      Reinicia o center manualmente para activar a nova versao."
}

Write-Host ""
Write-Host "============================================"
Write-Host " Versao $Version publicada com sucesso!"
Write-Host " Os agents actualizam-se automaticamente"
Write-Host " na proxima verificacao (ate 60 minutos)."
Write-Host "============================================"
Write-Host ""
