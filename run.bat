@echo off
setlocal EnableDelayedExpansion

echo ==========================================
echo       AIAC 2.0 Alpha-GPT Runner
echo ==========================================

REM Parse arguments
set "ACTION=restart"
set "PORT=8001"

:parse_args
if "%~1"=="" goto :end_parse
if /i "%~1"=="--start" set "ACTION=start"
if /i "%~1"=="--restart" set "ACTION=restart"
if /i "%~1"=="--stop" set "ACTION=stop"
if /i "%~1"=="--end" set "ACTION=stop"
if /i "%~1"=="--port" set "PORT=%~2" & shift
if /i "%~1"=="-h" goto :show_help
if /i "%~1"=="--help" goto :show_help
shift
goto :parse_args
:end_parse

echo [INFO] Action: %ACTION%
echo.

REM Execute action
if "%ACTION%"=="stop" goto :stop_services
if "%ACTION%"=="start" goto :start_services
if "%ACTION%"=="restart" goto :restart_services

goto :eof

:show_help
echo.
echo Usage: run.bat [OPTIONS]
echo.
echo Options:
echo   --start     Start services (skip if already running)
echo   --restart   Stop existing services and start fresh (default)
echo   --stop      Stop all services
echo   --end       Same as --stop
echo   --port NUM  Set backend port (default: 8001)
echo   -h, --help  Show this help message
echo.
goto :eof

:stop_services
echo [INFO] Stopping all AIAC services...

REM ---------------------------------------------------------------------------
REM Kill the PYTHON / NODE workers by COMMAND LINE — not by window title.
REM
REM Why (2026-05-23 root-cause): services launch via `start "AIAC Celery..." cmd
REM /k "...celery..."`. The window title lives on the cmd.exe WRAPPER, not on the
REM python.exe child. The old logic (a) `tasklist python.exe /v | findstr celery`
REM matched nothing — python's window title is not "celery" — and (b) `taskkill
REM /fi "WINDOWTITLE eq AIAC Celery*"` killed only the cmd wrapper, ORPHANING the
REM python worker. A pre-restart celery worker thus survived run.bat --restart and
REM served a mining session with stale code (task 3405: 42 alphas with NULL
REM dataset_id from the zombie worker). Matching the python/node command line
REM kills every worker regardless of how it was launched (run.bat OR manual dev
REM command), which tasklist cannot do (no command line) and wmic is deprecated
REM on Win11 — so we shell out to PowerShell + CIM.
REM ---------------------------------------------------------------------------

echo [INFO] Killing Celery workers + Beat (by command line)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'backend\.celery_app' } | ForEach-Object { Write-Host ('  [kill] celery PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [INFO] Killing Pool Supervisor + workers (by command line)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'backend\.pool\.(supervisor|run_worker)' } | ForEach-Object { Write-Host ('  [kill] pool PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [INFO] Killing Backend (uvicorn, by command line)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'uvicorn' } | ForEach-Object { Write-Host ('  [kill] backend PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [INFO] Killing Frontend (vite/npm, by command line)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' -and $_.CommandLine -match 'vite' } | ForEach-Object { Write-Host ('  [kill] frontend PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

REM Close the now-childless cmd.exe wrapper windows (cosmetic).
taskkill /fi "WINDOWTITLE eq AIAC Backend*" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq AIAC Frontend*" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq AIAC Celery*" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq AIAC Pool Supervisor*" /f >nul 2>&1

REM Port fallback: free the backend port if anything still holds it.
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%PORT% "') do (
    if not "%%a"=="0" (
        echo [INFO] Killing leftover process on port %PORT%: %%a
        taskkill /pid %%a /f >nul 2>&1
    )
)
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5173 :5174"') do (
    if not "%%a"=="0" (
        echo [INFO] Killing leftover process on frontend port: %%a
        taskkill /pid %%a /f >nul 2>&1
    )
)

REM Verify nothing survived — the orphan-worker bug was silent before, so make it loud.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Milliseconds 600; $left = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'backend\.celery_app|uvicorn' }); if ($left.Count -gt 0) { Write-Host ('[WARN] ' + $left.Count + ' service process(es) STILL ALIVE after kill: ' + ($left.ProcessId -join ', ') + ' — retrying'); $left | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } } else { Write-Host '[OK] verified: no celery/uvicorn python processes remain' }"

echo [OK] All services stopped.
if "%ACTION%"=="stop" goto :eof
goto :eof

:restart_services
call :stop_services
echo.
timeout /t 2 /nobreak >nul

:start_services
echo [INFO] Starting AIAC services...
echo.

REM 1. Check .env
if not exist ".env" (
    echo [INFO] .env not found. Creating from .env.example...
    copy .env.example .env
    echo [IMPORTANT] Please edit .env file to configure your credentials!
    notepad .env
    pause
)

REM 2. Setup virtual environment
if not exist "venv" (
    echo [INFO] Virtual environment not found. Creating...
    python -m venv venv
)

call venv\Scripts\activate

REM 3. Check dependencies
pip show fastapi >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing backend dependencies...
    pip install -r requirements.txt
) else (
    echo [OK] Backend dependencies ready.
)

if not exist "frontend\node_modules" (
    echo [INFO] Installing frontend dependencies...
    cd frontend
    call npm install
    cd ..
) else (
    echo [OK] Frontend dependencies ready.
)

REM 4. Check database
echo [INFO] Checking database connection...
python -c "from backend.config import settings; import psycopg2; conn = psycopg2.connect(host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, user=settings.POSTGRES_USER, password=settings.POSTGRES_PASSWORD, database='postgres'); cur = conn.cursor(); cur.execute('SELECT 1 FROM pg_database WHERE datname=%%s', (settings.POSTGRES_DB,)); exists = cur.fetchone(); conn.close(); exit(0 if exists else 1)" 2>nul
if errorlevel 1 (
    echo [INFO] Database not found. Creating...
    python backend/migrations/init_database.py
) else (
    echo [OK] Database connection verified.
)

REM 5. Run migrations
echo [INFO] Running database migrations...
cd backend
alembic upgrade head 2>nul
cd ..
echo [OK] Database migrations complete.

REM 6. Start services
echo.
echo [INFO] Starting Backend on port %PORT%...
start "AIAC Backend" cmd /k "call venv\Scripts\activate && uvicorn backend.main:app --reload --port %PORT%"

echo [INFO] Starting Frontend...
cd frontend
start "AIAC Frontend" cmd /k "npm run dev"
cd ..

REM Two Celery workers on separate queues (2026-05-21). Earlier this was 3
REM identical workers (BRAIN session thrash → zombie tasks), then 1 worker (no
REM thrash but single solo thread → a long/hung mining task starved the beat
REM maintenance tasks, so a frozen FLAT session could never be revived → permanent
REM RUNNING zombie). Fix: route run_mining_task to the `mining` queue (see
REM celery_app.task_routes) and run a dedicated `mining` worker, plus a `celery`
REM worker that always drains the default queue (watchdog/quota_guard/sync) even
REM while a long FLAT session occupies the mining worker. eb0d5a8 fleet-lock keeps
REM the two workers' shared BRAIN session coherent (no inter-process thrash).
echo [INFO] Starting Celery Workers (mining + maintenance)...
start "AIAC Celery Worker - Mining" cmd /k "call venv\Scripts\activate && celery -A backend.celery_app worker --loglevel=info --pool=solo -Q mining -n mining@%%h --logfile=.celery_worker_mining.log"
start "AIAC Celery Worker - Maint" cmd /k "call venv\Scripts\activate && celery -A backend.celery_app worker --loglevel=info --pool=solo -Q celery -n maint@%%h --logfile=.celery_worker_maint.log"

REM Start Celery Beat — drives the scheduled tasks declared in
REM celery_app.beat_schedule (V-19.7 watchdog every 5min, quota guard
REM every 10min, daily sync/refresh tasks). Without beat the watchdog
REM never fires and CONTINUOUS_CASCADE sessions stop being revived
REM after worker crashes.
echo [INFO] Starting Celery Beat (scheduler)...
start "AIAC Celery Beat" cmd /k "call venv\Scripts\activate && celery -A backend.celery_app beat --loglevel=info --logfile=.celery_beat.log"

REM Pool Supervisor — the Popen-respawn parent that launches the resident HG/S/E
REM worker subprocesses of the four-pool pipeline. It SELF-IDLES and exits at once
REM when ENABLE_POOL_PIPELINE is OFF (PoolSupervisor.run()'s guard), so it is safe
REM to always start; it only spawns workers after the Phase 1c cutover flips the
REM flag ON. Without this window the scheduler beat fills hyp_intent/candidate_queue
REM but NOTHING claims them — the pipeline silently produces zero alphas on flip.
echo [INFO] Starting Pool Supervisor (idle until ENABLE_POOL_PIPELINE)...
start "AIAC Pool Supervisor" cmd /k "call venv\Scripts\activate && python -m backend.pool.supervisor"

echo.
echo ==========================================
echo             Services Started!
echo ==========================================
echo.
echo   Backend:  http://localhost:%PORT%
echo   API Docs: http://localhost:%PORT%/docs
echo   Frontend: http://localhost:5174
echo.
echo   To stop: run.bat --stop
echo   To restart: run.bat --restart
echo ==========================================
goto :eof
