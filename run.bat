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

REM Kill uvicorn (backend)
for /f "tokens=2" %%a in ('tasklist /fi "imagename eq python.exe" /v 2^>nul ^| findstr /i "uvicorn"') do (
    echo [INFO] Killing backend process: %%a
    taskkill /pid %%a /f >nul 2>&1
)

REM Kill celery
for /f "tokens=2" %%a in ('tasklist /fi "imagename eq python.exe" /v 2^>nul ^| findstr /i "celery"') do (
    echo [INFO] Killing celery process: %%a
    taskkill /pid %%a /f >nul 2>&1
)

REM Kill node (frontend)
for /f "tokens=2" %%a in ('tasklist /fi "imagename eq node.exe" /v 2^>nul ^| findstr /i "vite"') do (
    echo [INFO] Killing frontend process: %%a
    taskkill /pid %%a /f >nul 2>&1
)

REM Kill by window title (fallback)
taskkill /fi "WINDOWTITLE eq AIAC Backend*" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq AIAC Frontend*" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq AIAC Celery*" /f >nul 2>&1

REM Kill processes on specific ports
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%PORT%"') do (
    if not "%%a"=="0" (
        echo [INFO] Killing process on port %PORT%: %%a
        taskkill /pid %%a /f >nul 2>&1
    )
)
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5173 :5174"') do (
    if not "%%a"=="0" (
        echo [INFO] Killing process on frontend port: %%a
        taskkill /pid %%a /f >nul 2>&1
    )
)

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

REM Start 3 Celery workers in parallel — Windows --pool=solo means
REM 1 task/worker, so 3 workers = 3 concurrent mining tasks. Hostnames
REM and logfiles ensure each worker is independently inspectable.
echo [INFO] Starting Celery Workers (3 instances)...
start "AIAC Celery Worker 1" cmd /k "call venv\Scripts\activate && celery -A backend.celery_app worker --loglevel=info --pool=solo -n worker1@%%h --logfile=.celery_worker1.log"
start "AIAC Celery Worker 2" cmd /k "call venv\Scripts\activate && celery -A backend.celery_app worker --loglevel=info --pool=solo -n worker2@%%h --logfile=.celery_worker2.log"
start "AIAC Celery Worker 3" cmd /k "call venv\Scripts\activate && celery -A backend.celery_app worker --loglevel=info --pool=solo -n worker3@%%h --logfile=.celery_worker3.log"

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
