@echo off
setlocal
pushd "%~dp0" || exit /b 1

set "PYTHON=%CD%\.venv\Scripts\python.exe"
set "LANGGRAPH=%CD%\.venv\Scripts\langgraph.exe"
set "SHOP_URL=http://127.0.0.1:8000"
set "PGWEB_URL=http://127.0.0.1:8081"
set "STUDIO_URL=http://127.0.0.1:2024"
set "PGWEB_CONTAINER=agent-sav-pgweb"

set "STEP=Trouver Python dans .venv (installer requirements.txt)"
if not exist "%PYTHON%" goto :error
set "STEP=Trouver LangGraph CLI (installer requirements-dev.txt)"
if not exist "%LANGGRAPH%" goto :error

set "STEP=Demarrer Docker Desktop"
docker info >nul 2>&1
if errorlevel 1 (
    echo Demarrage de Docker Desktop...
    docker desktop start --timeout 120 || goto :error
)

set "STEP=Arreter la boutique"
call :stop_service 8000 || goto :error

set "STEP=Arreter le serveur Studio"
call :stop_service 2024 || goto :error

set "STEP=Arreter Pgweb"
docker container inspect "%PGWEB_CONTAINER%" >nul 2>&1
if not errorlevel 1 (
    docker stop "%PGWEB_CONTAINER%" >nul || goto :error
)

set "STEP=Arreter PostgreSQL"
docker compose stop postgres || goto :error

set "STEP=Demarrer PostgreSQL"
docker compose up -d --wait --wait-timeout 60 postgres || goto :error

set "STEP=Demarrer Pgweb"
docker container inspect "%PGWEB_CONTAINER%" >nul 2>&1
if errorlevel 1 (
    call :create_pgweb || goto :error
) else (
    docker start "%PGWEB_CONTAINER%" >nul || goto :error
)

set "STEP=Demarrer la boutique"
start "Boutique Maison Orme" /min "%PYTHON%" -m agent_sav web || goto :error

set "STEP=Attendre la boutique sur le port 8000"
call :wait_for_http "%SHOP_URL%/api/customers" || goto :error

set "STEP=Demarrer le serveur Studio"
rem Le client LangSmith effectue des appels synchrones pendant le tracage local.
start "LangGraph Studio" /min "%PYTHON%" -m agent_sav.studio_dev --host 127.0.0.1 --port 2024 --no-browser --allow-blocking || goto :error

set "STEP=Attendre Pgweb sur le port 8081"
call :wait_for_http "%PGWEB_URL%/" || goto :error
set "STEP=Attendre le serveur Studio sur le port 2024"
call :wait_for_http "%STUDIO_URL%/ok" || goto :error

set "STEP=Ouvrir le sommaire"
if not exist "%~dp0interface\sommaire.html" goto :error
start "" "%~dp0interface\sommaire.html" || goto :error
popd
exit /b 0

:stop_service
powershell.exe -NoProfile -Command ^
    "$ErrorActionPreference = 'Stop';" ^
    "$listener = Get-NetTCPConnection -LocalAddress 127.0.0.1,0.0.0.0 -LocalPort %~1 -State Listen -ErrorAction SilentlyContinue;" ^
    "if (-not $listener) { exit 0 };" ^
    "$process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listener[0].OwningProcess);" ^
    "$pattern = '^\x22?' + [regex]::Escape($env:PYTHON) + '\x22?\s+';" ^
    "if (%~1 -eq 2024) { $pattern += '(?:-m\s+agent_sav\.studio_dev|\x22?' + [regex]::Escape($env:LANGGRAPH) + '\x22?\s+dev)(?:\s|$)' } else { $pattern += '-m\s+agent_sav\s+web(?:\s|$)' };" ^
    "if (%~1 -eq 2024) {" ^
    "    if ($process.CommandLine -notmatch $pattern -and $process.CommandLine -match 'multiprocessing\.spawn.+--multiprocessing-fork') {" ^
    "        $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $process.ParentProcessId);" ^
    "    };" ^
    "    if ($process.CommandLine -notmatch $pattern) { Write-Error 'Le port %~1 est occupe par un autre programme.' };" ^
    "    taskkill.exe /PID $process.ProcessId /T /F | Out-Null; exit $LASTEXITCODE;" ^
    "};" ^
    "if ($process.CommandLine -notmatch $pattern) { Write-Error 'Le port %~1 est occupe par un autre programme.' };" ^
    "Stop-Process -Id $process.ProcessId -PassThru | Wait-Process -Timeout 10"
exit /b %errorlevel%

:create_pgweb
set "POSTGRES_ID="
set "POSTGRES_NETWORK="
for /f %%I in ('docker compose ps -q postgres') do set "POSTGRES_ID=%%I"
if not defined POSTGRES_ID exit /b 1
for /f %%N in ('docker inspect --format "{{range .NetworkSettings.Networks}}{{println .NetworkID}}{{end}}" "%POSTGRES_ID%"') do set "POSTGRES_NETWORK=%%N"
if not defined POSTGRES_NETWORK exit /b 1
docker run -d --name "%PGWEB_CONTAINER%" --network "%POSTGRES_NETWORK%" ^
    -p 127.0.0.1:8081:8081 ^
    -e "PGWEB_DATABASE_URL=postgres://sav:sav@postgres:5432/sav?sslmode=disable" ^
    sosedoff/pgweb:0.17.0 --bind=0.0.0.0 --listen=8081 --readonly --lock-session
exit /b %errorlevel%

:wait_for_http
curl.exe --noproxy "*" -fs --max-time 3 --retry 60 --retry-all-errors --retry-delay 1 --retry-max-time 60 -o NUL "%~1"
exit /b %errorlevel%

:error
echo.
echo Echec : %STEP%.
popd
pause
exit /b 1
