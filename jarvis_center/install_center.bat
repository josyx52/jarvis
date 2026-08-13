@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1

:: ============================================================
::  JARVIS CENTER — Instalador Windows
::  Execute como Administrador
::  Requer: Python 3.11+  |  PostgreSQL  |  Redis
:: ============================================================

echo.
echo ============================================================
echo          JARVIS CENTER  ^|  Instalador
echo ============================================================
echo.

:: --- verificar admin ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] Execute como Administrador.
    pause & exit /b 1
)

set "SRC=%~dp0"
set "INSTALL_DIR=C:\JarvisCenter"
set "LOG_DIR=%INSTALL_DIR%\logs"
set "PY=python"

:: ============================================================
:: 1. PYTHON
:: ============================================================
echo [1/6] Verificar Python...
%PY% --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] Python nao encontrado. Instale Python 3.11+ e tente novamente.
    pause & exit /b 1
)
for /f "tokens=*" %%v in ('%PY% --version 2^>^&1') do echo        %%v
echo.

:: ============================================================
:: 2. CONFIGURACAO
:: ============================================================
echo [2/6] Configuracao
echo -------------------------------------------------------
echo.

set /p PG_HOST="  Host PostgreSQL        [localhost] : "
if "!PG_HOST!"=="" set PG_HOST=localhost

set /p PG_PORT="  Porta PostgreSQL        [5432]     : "
if "!PG_PORT!"=="" set PG_PORT=5432

set /p PG_DB="  Base de dados           [jarvis]   : "
if "!PG_DB!"=="" set PG_DB=jarvis

set /p PG_USER="  Utilizador PostgreSQL   [postgres]  : "
if "!PG_USER!"=="" set PG_USER=postgres

set /p PG_PASS="  Password PostgreSQL                : "

set /p REDIS_HOST="  Host Redis              [localhost] : "
if "!REDIS_HOST!"=="" set REDIS_HOST=localhost

set /p REDIS_PORT_IN="  Porta Redis             [6379]     : "
if "!REDIS_PORT_IN!"=="" set REDIS_PORT_IN=6379

set /p API_PORT_IN="  Porta API ingestao      [8080]     : "
if "!API_PORT_IN!"=="" set API_PORT_IN=8080

set /p FOUNDRY_KEY="  Azure Foundry API Key  (Enter=manter) : "

echo.

:: ============================================================
:: 3. COPIAR FICHEIROS
:: ============================================================
echo [3/6] A copiar ficheiros para %INSTALL_DIR% ...
if not exist "%INSTALL_DIR%"  mkdir "%INSTALL_DIR%"
if not exist "%LOG_DIR%"      mkdir "%LOG_DIR%"

xcopy /E /I /Y "%SRC%." "%INSTALL_DIR%\" >nul
echo        OK

:: --- patch start_center.py na copia instalada ---
set "SC=%INSTALL_DIR%\start_center.py"

%PY% -c "
import sys
path = sys.argv[1]
with open(path, encoding='utf-8') as f:
    c = f.read()
c = c.replace('POSTGRES_HOST = \"localhost\"',   'POSTGRES_HOST = \"%PG_HOST%\"')
c = c.replace('POSTGRES_PORT = \"5432\"',        'POSTGRES_PORT = \"%PG_PORT%\"')
c = c.replace('POSTGRES_DB = \"jarvis\"',        'POSTGRES_DB = \"%PG_DB%\"')
c = c.replace('POSTGRES_USER = \"postgres\"',    'POSTGRES_USER = \"%PG_USER%\"')
c = c.replace('POSTGRES_PASSWORD = \"Vermelho555@\"', 'POSTGRES_PASSWORD = \"%PG_PASS%\"')
c = c.replace('REDIS_HOST = \"localhost\"',      'REDIS_HOST = \"%REDIS_HOST%\"')
c = c.replace('REDIS_PORT = 6379',               'REDIS_PORT = %REDIS_PORT_IN%')
c = c.replace('API_PORT = 8080',                 'API_PORT = %API_PORT_IN%')
with open(path, 'w', encoding='utf-8') as f:
    f.write(c)
print('       start_center.py configurado')
" "%SC%"

:: --- patch foundry_client.py se chave fornecida ---
if not "!FOUNDRY_KEY!"=="" (
    set "FC=%INSTALL_DIR%\brain\foundry_client.py"
    %PY% -c "
import sys
path, key = sys.argv[1], sys.argv[2]
with open(path, encoding='utf-8') as f:
    c = f.read()
import re
c = re.sub(r'FOUNDRY_API_KEY\s*=\s*\"[^\"]*\"', 'FOUNDRY_API_KEY     = \"' + key + '\"', c)
with open(path, 'w', encoding='utf-8') as f:
    f.write(c)
print('       foundry_client.py configurado')
" "!FC!" "!FOUNDRY_KEY!"
)

:: ============================================================
:: 4. DEPENDENCIAS PYTHON
:: ============================================================
echo.
echo [4/6] A instalar dependencias Python...
%PY% -m pip install --upgrade pip -q
%PY% -m pip install fastapi "uvicorn[standard]" psycopg2-binary redis requests anthropic -q
if %errorlevel% neq 0 (
    echo [ERRO] Falha ao instalar dependencias.
    pause & exit /b 1
)
echo        OK

:: ============================================================
:: 5. BASE DE DADOS
:: ============================================================
echo.
echo [5/6] A inicializar base de dados...
set PGPASSWORD=!PG_PASS!

where psql >nul 2>&1
if %errorlevel% neq 0 (
    echo [AVISO] psql nao encontrado — a saltar criacao do schema.
    echo         Crie a base de dados manualmente e execute:
    echo         psql -U !PG_USER! -d !PG_DB! -f "%INSTALL_DIR%\storage\schema_init.sql"
    goto skip_db
)

:: criar DB se nao existir
psql -h !PG_HOST! -p !PG_PORT! -U !PG_USER! -tc "SELECT 1 FROM pg_database WHERE datname='!PG_DB!'" 2>nul | find "1" >nul
if %errorlevel% neq 0 (
    echo        A criar base de dados '!PG_DB!'...
    psql -h !PG_HOST! -p !PG_PORT! -U !PG_USER! -c "CREATE DATABASE ""!PG_DB!"" ENCODING 'UTF8';" >nul 2>&1
)

:: aplicar schema
psql -h !PG_HOST! -p !PG_PORT! -U !PG_USER! -d !PG_DB! -f "%INSTALL_DIR%\storage\schema_init.sql" >nul 2>&1
if %errorlevel% equ 0 (
    echo        Schema aplicado.
) else (
    echo [AVISO] Schema pode ter falhado. Verifique a ligacao ao PostgreSQL.
)

:skip_db

:: ============================================================
:: 6. SERVICO WINDOWS (NSSM)
:: ============================================================
echo.
echo [6/6] Instalar como servico Windows?
set /p INSTALL_SVC="       (s/n): "
if /i "!INSTALL_SVC!" neq "s" goto done

set "SVC=JarvisCenter"

where nssm >nul 2>&1
if %errorlevel% neq 0 (
    echo        NSSM nao encontrado. A tentar instalar via winget...
    winget install NSSM.NSSM -e --silent >nul 2>&1
    :: refrescar PATH
    for /f "skip=2 tokens=3*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SYS_PATH=%%a %%b"
    set "PATH=!SYS_PATH!;!PATH!"
    where nssm >nul 2>&1
    if %errorlevel% neq 0 (
        echo [AVISO] NSSM nao disponivel. Servico nao instalado.
        echo         Inicie manualmente: cd %INSTALL_DIR% ^& python start_center.py
        goto done
    )
)

sc query "!SVC!" >nul 2>&1
if %errorlevel% equ 0 (
    nssm stop  "!SVC!" >nul 2>&1
    nssm remove "!SVC!" confirm >nul 2>&1
)

nssm install "!SVC!" "%PY%" "%INSTALL_DIR%\start_center.py"
nssm set "!SVC!" AppDirectory    "%INSTALL_DIR%"
nssm set "!SVC!" AppStdout       "%LOG_DIR%\stdout.log"
nssm set "!SVC!" AppStderr       "%LOG_DIR%\stderr.log"
nssm set "!SVC!" AppRotateFiles  1
nssm set "!SVC!" AppRotateBytes  10485760
nssm set "!SVC!" Start           SERVICE_AUTO_START
nssm set "!SVC!" DisplayName     "Jarvis Center"
nssm set "!SVC!" Description     "Jarvis Center - observabilidade e analise AI"

net start "!SVC!" >nul 2>&1
if %errorlevel% equ 0 (
    echo        Servico '!SVC!' instalado e iniciado.
) else (
    echo        Servico instalado. Inicie com: net start !SVC!
)

:done
echo.
echo ============================================================
echo   INSTALACAO CONCLUIDA
echo ============================================================
echo.
echo   Directorio : %INSTALL_DIR%
echo   Logs       : %LOG_DIR%
echo.
echo   Iniciar manualmente:
echo     cd %INSTALL_DIR%
echo     python start_center.py
echo.
echo   API de ingestao : http://^<host^>:!API_PORT_IN!/telemetry
echo   Documentacao    : http://^<host^>:!API_PORT_IN!/docs
echo.
pause
endlocal
