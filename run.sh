#!/bin/bash

echo "=========================================="
echo "       AIAC 2.0 Alpha-GPT Runner"
echo "=========================================="

# Default values
ACTION="restart"
PORT=8001
PID_DIR=".pids"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --start)
            ACTION="start"
            shift
            ;;
        --restart)
            ACTION="restart"
            shift
            ;;
        --stop|--end)
            ACTION="stop"
            shift
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        -h|--help)
            echo ""
            echo "Usage: ./run.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --start     Start services (skip if already running)"
            echo "  --restart   Stop existing services and start fresh (default)"
            echo "  --stop      Stop all services"
            echo "  --end       Same as --stop"
            echo "  --port NUM  Set backend port (default: 8001)"
            echo "  -h, --help  Show this help message"
            echo ""
            exit 0
            ;;
        *)
            echo "[WARN] Unknown option: $1"
            shift
            ;;
    esac
done

echo "[INFO] Action: $ACTION"
echo ""

# Create PID directory
mkdir -p "$PID_DIR"

# Function to stop services
stop_services() {
    echo "[INFO] Stopping all AIAC services..."
    
    # Kill by PID files
    if [ -f "$PID_DIR/backend.pid" ]; then
        PID=$(cat "$PID_DIR/backend.pid")
        if kill -0 "$PID" 2>/dev/null; then
            echo "[INFO] Killing backend process: $PID"
            kill "$PID" 2>/dev/null
            kill -9 "$PID" 2>/dev/null
        fi
        rm -f "$PID_DIR/backend.pid"
    fi
    
    if [ -f "$PID_DIR/frontend.pid" ]; then
        PID=$(cat "$PID_DIR/frontend.pid")
        if kill -0 "$PID" 2>/dev/null; then
            echo "[INFO] Killing frontend process: $PID"
            kill "$PID" 2>/dev/null
            kill -9 "$PID" 2>/dev/null
        fi
        rm -f "$PID_DIR/frontend.pid"
    fi
    
    if [ -f "$PID_DIR/celery.pid" ]; then
        PID=$(cat "$PID_DIR/celery.pid")
        if kill -0 "$PID" 2>/dev/null; then
            echo "[INFO] Killing celery process: $PID"
            kill "$PID" 2>/dev/null
            kill -9 "$PID" 2>/dev/null
        fi
        rm -f "$PID_DIR/celery.pid"
    fi

    if [ -f "$PID_DIR/pool_supervisor.pid" ]; then
        PID=$(cat "$PID_DIR/pool_supervisor.pid")
        if kill -0 "$PID" 2>/dev/null; then
            echo "[INFO] Killing pool supervisor process: $PID"
            kill "$PID" 2>/dev/null
            kill -9 "$PID" 2>/dev/null
        fi
        rm -f "$PID_DIR/pool_supervisor.pid"
    fi

    # Kill by process name (fallback)
    pkill -f "uvicorn backend.main:app" 2>/dev/null
    pkill -f "celery -A backend.celery_app" 2>/dev/null
    pkill -f "vite.*frontend" 2>/dev/null
    pkill -f "backend.pool.supervisor" 2>/dev/null
    pkill -f "backend.pool.run_worker" 2>/dev/null
    
    # Kill by port (fallback)
    lsof -ti:$PORT | xargs kill -9 2>/dev/null
    lsof -ti:5173 | xargs kill -9 2>/dev/null
    lsof -ti:5174 | xargs kill -9 2>/dev/null
    
    sleep 1
    echo "[OK] All services stopped."
}

# Function to start services
start_services() {
    echo "[INFO] Starting AIAC services..."
    echo ""
    
    # 1. Check .env
    if [ ! -f ".env" ]; then
        echo "[INFO] .env not found. Creating from .env.example..."
        cp .env.example .env
        echo "[IMPORTANT] Please edit .env file to configure your credentials!"
        ${EDITOR:-nano} .env
    fi
    
    # 2. Setup virtual environment
    if [ ! -d "venv" ]; then
        echo "[INFO] Virtual environment not found. Creating..."
        python3 -m venv venv
    fi
    
    source venv/bin/activate
    
    # 3. Check dependencies
    if ! pip show fastapi > /dev/null 2>&1; then
        echo "[INFO] Installing backend dependencies..."
        pip install -r requirements.txt
    else
        echo "[OK] Backend dependencies ready."
    fi
    
    if [ ! -d "frontend/node_modules" ]; then
        echo "[INFO] Installing frontend dependencies..."
        cd frontend && npm install && cd ..
    else
        echo "[OK] Frontend dependencies ready."
    fi
    
    # 4. Check database
    echo "[INFO] Checking database connection..."
    python3 -c "
from backend.config import settings
import psycopg2
try:
    conn = psycopg2.connect(
        host=settings.POSTGRES_SERVER,
        port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
        database='postgres'
    )
    cur = conn.cursor()
    cur.execute('SELECT 1 FROM pg_database WHERE datname=%s', (settings.POSTGRES_DB,))
    exists = cur.fetchone()
    conn.close()
    exit(0 if exists else 1)
except:
    exit(1)
" 2>/dev/null
    
    if [ $? -ne 0 ]; then
        echo "[INFO] Database not found. Creating..."
        python backend/migrations/init_database.py
    else
        echo "[OK] Database connection verified."
    fi
    
    # 5. Run migrations
    echo "[INFO] Running database migrations..."
    cd backend && alembic upgrade head 2>/dev/null && cd ..
    echo "[OK] Database migrations complete."
    
    # 6. Start services
    echo ""
    echo "[INFO] Starting Backend on port $PORT..."
    source venv/bin/activate
    uvicorn backend.main:app --reload --port $PORT &
    echo $! > "$PID_DIR/backend.pid"
    
    echo "[INFO] Starting Frontend..."
    cd frontend && npm run dev &
    echo $! > "$PID_DIR/frontend.pid"
    cd ..
    
    echo "[INFO] Starting Celery Worker..."
    source venv/bin/activate
    celery -A backend.celery_app worker --loglevel=info &
    echo $! > "$PID_DIR/celery.pid"

    # Pool Supervisor — Popen-respawn parent for the resident HG/S/E worker
    # subprocesses. Self-idles + exits when ENABLE_POOL_PIPELINE is OFF, so it is
    # safe to always start; it only spawns workers after the Phase 1c cutover.
    echo "[INFO] Starting Pool Supervisor (idle until ENABLE_POOL_PIPELINE)..."
    source venv/bin/activate
    python -m backend.pool.supervisor &
    echo $! > "$PID_DIR/pool_supervisor.pid"

    echo ""
    echo "=========================================="
    echo "            Services Started!"
    echo "=========================================="
    echo ""
    echo "  Backend:  http://localhost:$PORT"
    echo "  API Docs: http://localhost:$PORT/docs"
    echo "  Frontend: http://localhost:5174"
    echo ""
    echo "  To stop: ./run.sh --stop"
    echo "  To restart: ./run.sh --restart"
    echo "  Press Ctrl+C to stop all services..."
    echo "=========================================="
    
    # Handle graceful shutdown
    trap "echo ''; echo '[INFO] Stopping services...'; stop_services; exit" SIGINT SIGTERM
    
    # Wait for any process to exit
    wait
}

# Execute action
case $ACTION in
    stop)
        stop_services
        ;;
    start)
        start_services
        ;;
    restart)
        stop_services
        echo ""
        sleep 2
        start_services
        ;;
esac
