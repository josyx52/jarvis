@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1

echo.
echo ============================================================
echo   BUILD  ^|  JarvisCenter-Installer.exe
echo ============================================================
echo.

:: --- verificar Python ---
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] Python nao encontrado.
    pause & exit /b 1
)

:: --- instalar PyInstaller ---
echo [1/3] A verificar PyInstaller...
python -m PyInstaller --version >nul 2>&1
if %errorlevel% neq 0 (
    echo       A instalar PyInstaller...
    python -m pip install pyinstaller -q
)
echo       OK

:: --- limpar builds anteriores ---
echo [2/3] A limpar builds anteriores...
if exist dist\JarvisCenterInstaller.exe del /f /q dist\JarvisCenterInstaller.exe
if exist build rmdir /s /q build
if exist JarvisCenterInstaller.spec del /f /q JarvisCenterInstaller.spec
echo       OK

:: --- build ---
echo [3/3] A compilar .exe (pode demorar 1-2 minutos)...
echo.

:: --onefile     : ficheiro .exe unico
:: --console     : janela de consola (instalador interactivo)
:: --name        : nome do executavel
:: --add-data    : embute todo o directorio jarvis_center dentro do .exe
::                 formato Windows: "origem;destino_no_bundle"

python -m PyInstaller ^
    --onefile ^
    --console ^
    --name "JarvisCenterInstaller" ^
    --add-data ".;jarvis_center" ^
    --exclude-module tkinter ^
    --exclude-module matplotlib ^
    --exclude-module numpy ^
    install_center.py

if %errorlevel% equ 0 (
    echo.
    echo ============================================================
    echo   BUILD CONCLUIDO
    echo ============================================================
    echo.
    echo   Instalador : %~dp0dist\JarvisCenterInstaller.exe
    echo.
    echo   Copie JarvisCenterInstaller.exe para a maquina de destino
    echo   e execute como Administrador.
    echo.
) else (
    echo.
    echo [ERRO] Build falhou. Verifique os erros acima.
    echo.
)

pause
endlocal
