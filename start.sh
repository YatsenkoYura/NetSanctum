#!/usr/bin/env bash

# ── NetSanctum Management & Launch Script ─────────────────────────────────────
# Enables seamless startup with configurable ports via .env or CLI arguments.
#
# Usage:
#   ./start.sh                  # Start on port specified in .env (default: 8000)
#   ./start.sh 4000             # Start on port 4000 (updates .env automatically)
#   ./start.sh -p 5000          # Start on port 5000
#   ./start.sh --down           # Stop all containers cleanly
#   ./start.sh --logs           # Tail container logs
#   ./start.sh --no-browser-runtime # Start without Chromium runtime/proxy
# ──────────────────────────────────────────────────────────────────────────────

set -e
umask 077

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

ENV_FILE=".env"
ENV_EXAMPLE=".env.example"
CREATED_ENV=0

# Ensure .env exists
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$ENV_EXAMPLE" ]; then
        echo "Creating .env from .env.example..."
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        CREATED_ENV=1
    else
        echo "Error: Neither .env nor .env.example found!"
        exit 1
    fi
fi

if [ "$CREATED_ENV" = "1" ]; then
    DB_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    API_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    sed -i "s/change_me_in_production/$DB_SECRET/g" "$ENV_FILE"
    sed -i "s/dev-api-key-change-me/$API_SECRET/g" "$ENV_FILE"
fi

if grep -q 'change_me_in_production' "$ENV_FILE"; then
    DB_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    sed -i "s/change_me_in_production/$DB_SECRET/g" "$ENV_FILE"
fi

if grep -q '^MASTER_API_KEY=dev-api-key-change-me$' "$ENV_FILE"; then
    API_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    sed -i "s/^MASTER_API_KEY=.*/MASTER_API_KEY=$API_SECRET/" "$ENV_FILE"
fi

chmod 600 "$ENV_FILE"
if ! grep -q '^PUID=' "$ENV_FILE"; then
    echo "PUID=$(id -u)" >> "$ENV_FILE"
fi
if ! grep -q '^PGID=' "$ENV_FILE"; then
    echo "PGID=$(id -g)" >> "$ENV_FILE"
fi

# Parse CLI arguments
PORT_ARG=""
ACTION="up"
BROWSER_RUNTIME=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--port)
            PORT_ARG="$2"
            shift 2
            ;;
        --down|stop)
            ACTION="down"
            shift
            ;;
        --logs|logs)
            ACTION="logs"
            shift
            ;;
        --restart|restart)
            ACTION="restart"
            shift
            ;;
        --no-browser-runtime)
            BROWSER_RUNTIME=0
            shift
            ;;
        --browser-runtime)
            BROWSER_RUNTIME=1
            shift
            ;;
        *)
            if [[ "$1" =~ ^[0-9]+$ ]]; then
                PORT_ARG="$1"
                shift
            else
                echo "Unknown argument: $1"
                echo "Usage: ./start.sh [PORT] [-p PORT] [--down] [--logs] [--restart] [--no-browser-runtime]"
                exit 1
            fi
            ;;
    esac
done

if [ "$ACTION" = "down" ]; then
    echo "Stopping NetSanctum containers..."
    docker compose --profile browser down --remove-orphans
    echo "NetSanctum stopped."
    exit 0
fi

if [ "$ACTION" = "logs" ]; then
    docker compose --profile browser logs -f --tail=100
    exit 0
fi

# If a port was specified, update HOST_PORT in .env
if [ -n "$PORT_ARG" ]; then
    if grep -q "^HOST_PORT=" "$ENV_FILE"; then
        # Replace existing HOST_PORT setting
        sed -i "s/^HOST_PORT=.*/HOST_PORT=$PORT_ARG/" "$ENV_FILE"
    else
        # Append HOST_PORT setting if missing
        echo "" >> "$ENV_FILE"
        echo "HOST_PORT=$PORT_ARG" >> "$ENV_FILE"
    fi
    echo "Updated HOST_PORT=$PORT_ARG in $ENV_FILE"
fi

# Read HOST_PORT from .env (fallback to 8000)
HOST_PORT=$(grep -E "^HOST_PORT=" "$ENV_FILE" | cut -d'=' -f2 | tr -d ' "'$'\r' || true)
HOST_PORT="${HOST_PORT:-8000}"

echo "========================================================"
echo " Starting NetSanctum on host port: $HOST_PORT"
echo " Configuration file: $ENV_FILE"
if [ "$BROWSER_RUNTIME" = "1" ]; then
    echo " Browser runtime: enabled (on-demand Chromium)"
else
    echo " Browser runtime: disabled"
fi
echo "========================================================"

# Clean up stale/orphaned containers first to prevent DNS/network conflicts
if [ "$ACTION" = "restart" ]; then
    echo "Recreating containers..."
    docker compose --profile browser down --remove-orphans
fi

# Check if port is already bound on host before launching
if command -v lsof >/dev/null 2>&1; then
    if lsof -i :"$HOST_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "WARNING: Host port $HOST_PORT appears to be in use."
        echo "Cleaning up existing containers..."
        docker compose --profile browser down --remove-orphans
    fi
fi

# Launch containers
echo "Building and launching Docker services..."
if [ "$BROWSER_RUNTIME" = "1" ]; then
    BROWSER_RUNTIME_ENABLED=1 docker compose --profile browser up -d --build --remove-orphans
else
    docker compose --profile browser stop browser-runtime browser-proxy >/dev/null 2>&1 || true
    docker compose --profile browser rm -f browser-runtime browser-proxy >/dev/null 2>&1 || true
    BROWSER_RUNTIME_ENABLED=0 docker compose up -d --build --remove-orphans
fi

echo ""
echo "========================================================"
echo " NetSanctum is running!"
echo " Web UI: http://localhost:$HOST_PORT"
echo " API Docs: http://localhost:$HOST_PORT/docs"
echo "========================================================"
echo ""
echo "To view logs:  ./start.sh --logs"
echo "To stop:       ./start.sh --down"
echo "========================================================"
