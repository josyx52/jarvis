@echo off
:: ============================================================
::  JARVIS CENTER — Instalar dependencias Python
::  Instala SEMPRE em C:\Python313 (sistema), nunca no perfil
::  Execute como Administrador
:: ============================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] Execute como Administrador.
    pause & exit /b 1
)

set "PY=C:\Python313\python.exe"

if not exist "%PY%" (
    echo [ERRO] %PY% nao encontrado.
    echo        Ajuste a variavel PY no topo deste ficheiro.
    pause & exit /b 1
)

echo.
echo Python: %PY%
%PY% --version
echo.
echo A instalar em:
%PY% -c "import site; print(site.getsitepackages()[0])"
echo.

:: --no-user garante instalacao no Python de sistema, nao no perfil
%PY% -m pip install --upgrade pip --no-user
%PY% -m pip install --upgrade --no-user -r "%~dp0requirements.txt"

if %errorlevel% equ 0 (
    echo.
    echo [OK] Pacotes instalados com sucesso.
    echo.
    %PY% -c "import redis, anthropic, fastapi; print('redis:', redis.__file__); print('anthropic:', anthropic.__file__); print('fastapi:', fastapi.__file__)"
) else (
    echo.
    echo [ERRO] Falha na instalacao. Ver mensagens acima.
)

echo.
pause
