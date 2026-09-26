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
#   ./start.sh --no-agent       # Start without the assistant runtime
#   ./start.sh --miku-local      # Start the optional local llama.cpp model
#   ./start.sh --chown-abort     # Hand ./storage back to you, access restored
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

# An .env written before a feature branch existed is missing the settings that branch
# added, and compose then fails inside a one-shot container with an opaque exit code:
# the model fetcher simply exits 1 because it was told no file and no URL. Copy across
# every documented key that is absent, before the secret pass below replaces the
# placeholders, and never touch a value the operator has already set.
if [ -f "$ENV_EXAMPLE" ]; then
    BACKFILLED=""
    while IFS= read -r EXAMPLE_LINE || [ -n "$EXAMPLE_LINE" ]; do
        case "$EXAMPLE_LINE" in
            ''|\#*) continue ;;
            *=*) ;;
            *) continue ;;
        esac
        EXAMPLE_KEY="${EXAMPLE_LINE%%=*}"
        EXAMPLE_VALUE="${EXAMPLE_LINE#*=}"
        case "$EXAMPLE_KEY" in
            ''|*[!A-Za-z0-9_]*) continue ;;
        esac
        # An empty example value documents a knob rather than a default: adding it
        # would put a blank line into a working .env and hide the value that compose
        # or the application already defaults on its own.
        if [ -z "$EXAMPLE_VALUE" ]; then
            continue
        fi
        # Placeholders are not defaults. Copying "change_me" into a real .env would
        # hand the deployment a published password or encryption key, so those keys
        # are left to the secret pass below, which generates a real value.
        case "$EXAMPLE_VALUE" in
            *change_me*|dev-*) continue ;;
        esac
        if ! grep -q "^${EXAMPLE_KEY}=" "$ENV_FILE"; then
            printf '%s\n' "$EXAMPLE_LINE" >> "$ENV_FILE"
            BACKFILLED="$BACKFILLED $EXAMPLE_KEY"
        fi
    done < "$ENV_EXAMPLE"
    if [ -n "$BACKFILLED" ]; then
        echo "Added missing settings from .env.example:$BACKFILLED"
    fi
fi

if grep -q 'change_me_in_production' "$ENV_FILE"; then
    DB_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    sed -i "s/change_me_in_production/$DB_SECRET/g" "$ENV_FILE"
fi

if grep -q '^MASTER_API_KEY=dev-api-key-change-me$' "$ENV_FILE"; then
    API_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    sed -i "s/^MASTER_API_KEY=.*/MASTER_API_KEY=$API_SECRET/" "$ENV_FILE"
fi

for AGENT_SECRET_NAME in AGENT_RUNTIME_TOKEN AGENT_INTERNAL_KEY; do
    if ! grep -q "^${AGENT_SECRET_NAME}=" "$ENV_FILE" || grep -q "^${AGENT_SECRET_NAME}=dev-${AGENT_SECRET_NAME,,}-change-me$" "$ENV_FILE"; then
        AGENT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
        if grep -q "^${AGENT_SECRET_NAME}=" "$ENV_FILE"; then
            sed -i "s/^${AGENT_SECRET_NAME}=.*/${AGENT_SECRET_NAME}=$AGENT_SECRET/" "$ENV_FILE"
        else
            printf '\n%s=%s\n' "$AGENT_SECRET_NAME" "$AGENT_SECRET" >> "$ENV_FILE"
        fi
    fi
done

if ! grep -q '^REDIS_PASSWORD=' "$ENV_FILE" || grep -q '^REDIS_PASSWORD=change_me_redis_password$' "$ENV_FILE"; then
    REDIS_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    if grep -q '^REDIS_PASSWORD=' "$ENV_FILE"; then
        sed -i "s/^REDIS_PASSWORD=.*/REDIS_PASSWORD=$REDIS_SECRET/" "$ENV_FILE"
    else
        printf '\nREDIS_PASSWORD=%s\n' "$REDIS_SECRET" >> "$ENV_FILE"
    fi
    sed -i "s/change_me_redis_password/$REDIS_SECRET/g" "$ENV_FILE"
fi

chmod 600 "$ENV_FILE"
# Which host user the containers hand ./storage to. Filled in rather than shipped as
# a value because a wrong number here is invisible until the storage directory stops
# belonging to the person running the script, and a number copied from an example
# file is wrong on every host whose user is not the first one created.
if ! grep -q '^PUID=' "$ENV_FILE"; then
    echo "PUID=$(id -u)" >> "$ENV_FILE"
fi
if ! grep -q '^PGID=' "$ENV_FILE"; then
    echo "PGID=$(id -g)" >> "$ENV_FILE"
fi
# A .env written before these were filled in can carry a uid that is not this user's,
# and the symptom is not a message about users: the storage directory simply stops
# belonging to the person running the script. Said here, where the cause is known.
CONFIGURED_PUID="$(sed -n 's/^PUID=//p' "$ENV_FILE" | tail -1)"
CONFIGURED_PGID="$(sed -n 's/^PGID=//p' "$ENV_FILE" | tail -1)"
if [ "$CONFIGURED_PUID" != "$(id -u)" ] || [ "$CONFIGURED_PGID" != "$(id -g)" ]; then
    echo "WARNING: $ENV_FILE says PUID=$CONFIGURED_PUID PGID=$CONFIGURED_PGID,"
    echo "         but you are $(id -un) (uid $(id -u), gid $(id -g))."
    echo "         The containers hand ./storage to that uid, so it may not be yours to write."
    echo "         To fix it, replace those two lines in $ENV_FILE with:"
    echo "           PUID=$(id -u)"
    echo "           PGID=$(id -g)"
fi

# Parse CLI arguments
PORT_ARG=""
ACTION="up"
BROWSER_RUNTIME=1
MIKU_LOCAL=0
AGENT_RUNTIME=1
VOICE=0
if grep -q '^MIKU_LLM_URL=http://miku-llm:' "$ENV_FILE"; then
    MIKU_LOCAL=1
fi
RECREATE_ARGS=()

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
        --chown-abort|chown-abort)
            ACTION="chown-abort"
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
        --miku-local)
            AGENT_RUNTIME=1
            MIKU_LOCAL=1
            shift
            ;;
        --no-miku-local)
            MIKU_LOCAL=0
            shift
            ;;
        --voice)
            VOICE=1
            shift
            ;;
        --no-voice)
            VOICE=0
            shift
            ;;
        --no-agent)
            AGENT_RUNTIME=0
            shift
            ;;
        *)
            if [[ "$1" =~ ^[0-9]+$ ]]; then
                PORT_ARG="$1"
                shift
            else
                echo "Unknown argument: $1"
                echo "Usage: ./start.sh [PORT] [-p PORT] [--down] [--logs] [--restart] [--chown-abort] [--no-browser-runtime] [--miku-local] [--voice] [--no-agent]"
                exit 1
            fi
            ;;
    esac
done

if [ "$ACTION" = "down" ]; then
    echo "Stopping NetSanctum containers..."
    docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile browser --profile agent --profile miku-local down --remove-orphans 2>/dev/null \
        || docker compose --profile browser --profile agent --profile miku-local down --remove-orphans
    echo "NetSanctum stopped."
    exit 0
fi

if [ "$ACTION" = "logs" ]; then
    docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile browser --profile agent --profile miku-local logs -f --tail=100 2>/dev/null \
        || docker compose --profile browser --profile agent --profile miku-local logs -f --tail=100
    exit 0
fi

if [ "$ACTION" = "chown-abort" ]; then
    # A repair, not a start. The storage directory is handed to a uid read from
    # .env and then locked to that uid alone, so a host whose user is not that
    # number - or one that has since changed - is left unable to read its own data
    # with no way back in short of root. The fix is the one the stack already has
    # the means to make, run on its own: a container started as root, changing the
    # ownership the containers themselves would have set.
    REPAIR_IMAGE="alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"
    TARGET_DIR="${MIKU_STORAGE_DIR:-./storage}"
    case "$TARGET_DIR" in
        /*) ABS_DIR="$TARGET_DIR" ;;
        *) ABS_DIR="$PROJECT_DIR/${TARGET_DIR#./}" ;;
    esac
    if [ ! -d "$ABS_DIR" ]; then
        mkdir -p "$ABS_DIR"
    fi
    echo "Restoring access to $TARGET_DIR for $(id -un) (uid $(id -u))..."
    echo "  This hands the directory back to you and reopens it to every local user,"
    echo "  which includes the admin token hash and the browser session snapshots."
    echo "  It is a deliberate loosening, and ./start.sh will not undo it afterwards."
    docker run --rm --user 0:0 \
        -e TARGET_UID="$(id -u)" -e TARGET_GID="$(id -g)" \
        -v "$ABS_DIR:/target" \
        "$REPAIR_IMAGE" sh -ec 'chown -R "$TARGET_UID:$TARGET_GID" /target && chmod -R a+rwX /target'
    if [ $? -ne 0 ]; then
        echo "Error: could not change $TARGET_DIR. Check that the docker daemon is running."
        exit 1
    fi
    echo "Done. $(ls -ld "$ABS_DIR" | awk '{print $1, $3":"$4}')"
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
if [ "$AGENT_RUNTIME" = "1" ]; then
    echo " Agent runtime: enabled (isolated cascade executor)"
else
    echo " Agent runtime: disabled"
fi
if [ "$MIKU_LOCAL" = "1" ]; then
    echo " Local model: enabled (llama.cpp)"
fi
if [ "$VOICE" = "1" ]; then
    echo " Voice runtime: enabled (server-side recognition and synthesis)"
else
    echo " Voice runtime: disabled (browser speech, no server cost)"
fi
echo "========================================================"

if [ "$ACTION" = "restart" ]; then
    echo "Recreating application containers after a successful build..."
    RECREATE_ARGS=(--force-recreate)
fi

# Check if port is already bound on host before launching
if command -v lsof >/dev/null 2>&1; then
    if lsof -i :"$HOST_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "WARNING: Host port $HOST_PORT appears to be in use."
        echo "Docker Compose will reuse or replace the existing NetSanctum service."
    fi
fi

# Launch containers
# Fail here, with the reason, rather than inside a one-shot container that exits 1 and
# leaves the model host without the weights it waits for.

# Said here rather than left to mkdir, which reports "Permission denied" without
# saying which directory, which user, or what to do about it - and the voice profile
# cannot start without these weights, so the message is the whole diagnosis.
voice_model_dir_unusable() {
    echo "Error: $VOICE_MODEL_DIR is not usable by the user running this script."
    echo "  user:   $(id -un) (uid $(id -u))"
    echo "  path:   $VOICE_MODEL_DIR"
    if [ -L "./storage" ]; then
        # The shape that reports the parent rather than the child: storage is a
        # symlink, so the refusal is about wherever it points, and it is invisible
        # in the path above.
        echo "  note:   ./storage is a symlink to $(readlink -f ./storage 2>/dev/null || echo an unresolvable target)"
    fi
    if [ -e "$VOICE_MODEL_DIR" ]; then
        echo "  owner:  $(stat -c '%U:%G (mode %a)' "$VOICE_MODEL_DIR" 2>/dev/null || echo unknown)"
    fi
    echo "  The voice models are about a gigabyte and have to land somewhere writable."
    echo "  Either hand the directory to that user:"
    echo "    sudo chown -R \"\$(id -u):\$(id -g)\" \"$VOICE_MODEL_DIR\""
    echo "  or point the models at a path you already own, in .env. Write it out in"
    echo "  full: compose reads this file literally and does not expand ~ :"
    echo "    MIKU_VOICE_MODEL_DIR=$HOME/netsanctum-voice-models"
    exit 1
}

VOICE_MODEL_DIR="${MIKU_VOICE_MODEL_DIR:-./storage/voice-models}"
if [ "$VOICE" = "1" ] && [ "$ACTION" = "up" ]; then
    if [ ! -d "$VOICE_MODEL_DIR" ] && ! mkdir -p "$VOICE_MODEL_DIR" 2>/dev/null; then
        voice_model_dir_unusable
    elif [ -d "$VOICE_MODEL_DIR" ] && [ ! -w "$VOICE_MODEL_DIR" ]; then
        # The directory is there but belongs to someone else, which is what a stack
        # first started with sudo leaves behind. Creating it would have succeeded, so
        # this case is the one a bare mkdir never reports.
        voice_model_dir_unusable
    fi
fi
if [ "$MIKU_LOCAL" = "1" ] && [ "$ACTION" = "up" ]; then
    MODEL_FILE="$(sed -n 's/^MIKU_MODEL_FILE=//p' "$ENV_FILE" | tail -1)"
    MODEL_URL="$(sed -n 's/^MIKU_MODEL_URL=//p' "$ENV_FILE" | tail -1)"
    MODEL_DIR="$(sed -n 's/^MIKU_MODEL_DIR=//p' "$ENV_FILE" | tail -1)"
    MODEL_DIR="${MODEL_DIR:-./storage/models}"
    if [ -z "$MODEL_FILE" ] || [ -z "$MODEL_URL" ]; then
        echo "Error: the local model needs both MIKU_MODEL_FILE and MIKU_MODEL_URL in .env"
        echo "  Either fill them in, or place the weights at $MODEL_DIR/$MODEL_FILE yourself."
        exit 1
    fi
    if [ ! -s "$MODEL_DIR/$MODEL_FILE" ]; then
        AVAILABLE="$(df -Pk "$MODEL_DIR" 2>/dev/null | awk 'NR==2 {print int($4/1024/1024)}')"
        if [ -n "$AVAILABLE" ] && [ "$AVAILABLE" -lt 2 ]; then
            echo "Error: $MODEL_DIR has less than 2 GB free, which is not enough for the model"
            exit 1
        fi
    fi
fi

echo "Building and launching Docker services..."
COMPOSE_FILES=(-f docker-compose.yml)
# A GPU is opt-in: only mount /dev/dri when the host actually has one, so the same
# stack boots on a headless server without it.
if [ -n "$MIKU_LOCAL" ] && [ -e /dev/dri ]; then
    COMPOSE_FILES+=(-f docker-compose.gpu.yml)
    export MIKU_LLM_THREADS="${MIKU_LLM_THREADS:-$(nproc 2>/dev/null || echo 4)}"
    export MIKU_LLM_CPUS="${MIKU_LLM_CPUS:-$(nproc 2>/dev/null || echo 4)}"
    echo " GPU: detected /dev/dri, model will be served with acceleration"
elif [ "$MIKU_LOCAL" = "1" ]; then
    export MIKU_LLM_THREADS="${MIKU_LLM_THREADS:-$(nproc 2>/dev/null || echo 4)}"
    export MIKU_LLM_CPUS="${MIKU_LLM_CPUS:-$(nproc 2>/dev/null || echo 4)}"
    echo " GPU: none found, model will be served on CPU"
fi
PROFILE_ARGS=()
if [ "$BROWSER_RUNTIME" = "1" ]; then
    PROFILE_ARGS+=(--profile browser)
else
    docker compose "${COMPOSE_FILES[@]}" --profile browser stop browser-runtime browser-proxy >/dev/null 2>&1 || true
    docker compose "${COMPOSE_FILES[@]}" --profile browser rm -f browser-runtime browser-proxy >/dev/null 2>&1 || true
fi
if [ "$MIKU_LOCAL" = "1" ]; then
    PROFILE_ARGS+=(--profile miku-local)
else
    docker compose "${COMPOSE_FILES[@]}" --profile miku-local stop miku-llm >/dev/null 2>&1 || true
    docker compose "${COMPOSE_FILES[@]}" --profile miku-local rm -f miku-llm model-init >/dev/null 2>&1 || true
fi
if [ "$VOICE" = "1" ]; then
    PROFILE_ARGS+=(--profile voice)
else
    # Stopped and removed, not just left unused: the point of the browser mode is
    # that the server stops holding a speech model in memory.
    docker compose "${COMPOSE_FILES[@]}" --profile voice stop miku-voice miku-stt voice-init >/dev/null 2>&1 || true
    docker compose "${COMPOSE_FILES[@]}" --profile voice rm -f miku-voice miku-stt voice-init >/dev/null 2>&1 || true
fi
if [ "$AGENT_RUNTIME" = "1" ]; then
    PROFILE_ARGS+=(--profile agent)
else
    docker compose "${COMPOSE_FILES[@]}" --profile agent stop agent-runtime >/dev/null 2>&1 || true
    docker compose "${COMPOSE_FILES[@]}" --profile agent rm -f agent-runtime >/dev/null 2>&1 || true
fi
AGENT_RUNTIME_ENABLED="$AGENT_RUNTIME" BROWSER_RUNTIME_ENABLED="$BROWSER_RUNTIME" docker compose "${COMPOSE_FILES[@]}" "${PROFILE_ARGS[@]}" build
AGENT_RUNTIME_ENABLED="$AGENT_RUNTIME" BROWSER_RUNTIME_ENABLED="$BROWSER_RUNTIME" docker compose "${COMPOSE_FILES[@]}" "${PROFILE_ARGS[@]}" up -d --remove-orphans "${RECREATE_ARGS[@]}"

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
