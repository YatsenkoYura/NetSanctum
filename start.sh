#!/usr/bin/env bash

# ── NetSanctum Management & Launch Script ─────────────────────────────────────
# Starts the stack, and without arguments opens an interactive menu: stack
# status, feature toggles, and a small .env editor. Every flag below runs the
# same actions non-interactively, so scripts and docs keep working.
#
# Usage:
#   ./start.sh                  # Interactive menu
#   ./start.sh up               # Start immediately (no menu)
#   ./start.sh 4000             # Start on port 4000 (updates .env automatically)
#   ./start.sh -p 5000          # Start on port 5000
#   ./start.sh --down           # Stop all containers cleanly
#   ./start.sh --logs           # Tail container logs
#   ./start.sh --restart        # Rebuild and recreate the application containers
#   ./start.sh --no-browser-runtime # Start without Chromium runtime/proxy
#   ./start.sh --no-agent       # Start without the assistant runtime
#   ./start.sh --miku-local     # Start the optional local llama.cpp model
#   ./start.sh --voice          # Start with server-side voice recognition
#   ./start.sh --chown-abort    # Hand ./storage back to you, access restored
# ──────────────────────────────────────────────────────────────────────────────

set -e
umask 077

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

ENV_FILE=".env"
ENV_EXAMPLE=".env.example"
CREATED_ENV=0
# Notices from the bootstrap below (backfills, secret generation, ownership
# warnings). They are printed as they happen for the CLI path and replayed in
# the menu header, because the menu clears the screen they were printed to.
ENV_NOTICES=""
note() {
    printf '%s\n' "$1"
    ENV_NOTICES="${ENV_NOTICES}${1}"$'\n'
}

# ── .env helpers ──────────────────────────────────────────────────────────────
# All writes go through these two: they replace only the targeted line, so
# comments, blank lines and key order in a hand-maintained .env survive every
# edit the menu makes. Values pass through awk's ENVIRON rather than -v or sed
# so a value containing /, &, | or \ cannot be read as replacement syntax.

env_get() {
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -1
}

env_set() {
    ENV_KEY="$1" ENV_VALUE="$2" awk '
        BEGIN { key = ENVIRON["ENV_KEY"]; val = ENVIRON["ENV_VALUE"]; seen = 0 }
        !seen && index($0, key "=") == 1 { print key "=" val; seen = 1; next }
        { print }
        END { if (!seen) print key "=" val }
    ' "$ENV_FILE" > "${ENV_FILE}.tmp" && mv "${ENV_FILE}.tmp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

# Literal text replacement across the whole file, for the coupled keys: a
# password appears in POSTGRES_PASSWORD and inside two database URLs, and sed
# regex rules would eat values that happen to contain its metacharacters.
replace_all_literal() {
    REPLACE_FROM="$1" REPLACE_TO="$2" awk '
        BEGIN { from = ENVIRON["REPLACE_FROM"]; to = ENVIRON["REPLACE_TO"] }
        {
            line = $0; out = ""
            while ((i = index(line, from)) > 0) {
                out = out substr(line, 1, i - 1) to
                line = substr(line, i + length(from))
            }
            print out line
        }
    ' "$ENV_FILE" > "${ENV_FILE}.tmp" && mv "${ENV_FILE}.tmp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

gen_secret() {
    if command -v python >/dev/null 2>&1; then
        python -c 'import secrets; print(secrets.token_urlsafe(32))'
    else
        python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
    fi
}

# Ensure .env exists
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$ENV_EXAMPLE" ]; then
        note "Creating .env from .env.example..."
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        CREATED_ENV=1
    else
        echo "Error: Neither .env nor .env.example found!"
        exit 1
    fi
fi

if [ "$CREATED_ENV" = "1" ]; then
    DB_SECRET="$(gen_secret)"
    API_SECRET="$(gen_secret)"
    sed -i "s/change_me_in_production/$DB_SECRET/g" "$ENV_FILE"
    sed -i "s/dev-api-key-change-me/$API_SECRET/" "$ENV_FILE"
fi

# An .env written before a feature branch existed is missing the settings that
# branch added, and compose then fails inside a one-shot container with an
# opaque exit code: the model fetcher simply exits 1 because it was told no
# file and no URL. Copy across every documented key that is absent, before the
# secret pass below replaces the placeholders, and never touch a value the
# operator has already set.
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
        # An empty example value documents a knob rather than a default: adding
        # it would put a blank line into a working .env and hide the value that
        # compose or the application already defaults on its own.
        if [ -z "$EXAMPLE_VALUE" ]; then
            continue
        fi
        # Placeholders are not defaults. Copying "change_me" into a real .env
        # would hand the deployment a published password or encryption key, so
        # those keys are left to the secret pass below, which generates a real
        # value.
        case "$EXAMPLE_VALUE" in
            *change_me*|dev-*) continue ;;
        esac
        if ! grep -q "^${EXAMPLE_KEY}=" "$ENV_FILE"; then
            printf '%s\n' "$EXAMPLE_LINE" >> "$ENV_FILE"
            BACKFILLED="$BACKFILLED $EXAMPLE_KEY"
        fi
    done < "$ENV_EXAMPLE"
    if [ -n "$BACKFILLED" ]; then
        note "Added missing settings from .env.example:$BACKFILLED"
    fi
fi

if grep -q 'change_me_in_production' "$ENV_FILE"; then
    DB_SECRET="$(gen_secret)"
    sed -i "s/change_me_in_production/$DB_SECRET/g" "$ENV_FILE"
fi

if grep -q '^MASTER_API_KEY=dev-api-key-change-me$' "$ENV_FILE"; then
    API_SECRET="$(gen_secret)"
    sed -i "s/^MASTER_API_KEY=.*/MASTER_API_KEY=$API_SECRET/" "$ENV_FILE"
fi

for AGENT_SECRET_NAME in AGENT_RUNTIME_TOKEN AGENT_INTERNAL_KEY; do
    if ! grep -q "^${AGENT_SECRET_NAME}=" "$ENV_FILE" || grep -q "^${AGENT_SECRET_NAME}=dev-${AGENT_SECRET_NAME,,}-change-me$" "$ENV_FILE"; then
        AGENT_SECRET="$(gen_secret)"
        if grep -q "^${AGENT_SECRET_NAME}=" "$ENV_FILE"; then
            sed -i "s/^${AGENT_SECRET_NAME}=.*/${AGENT_SECRET_NAME}=$AGENT_SECRET/" "$ENV_FILE"
        else
            printf '\n%s=%s\n' "$AGENT_SECRET_NAME" "$AGENT_SECRET" >> "$ENV_FILE"
        fi
    fi
done

if ! grep -q '^REDIS_PASSWORD=' "$ENV_FILE" || grep -q '^REDIS_PASSWORD=change_me_redis_password$' "$ENV_FILE"; then
    REDIS_SECRET="$(gen_secret)"
    if grep -q '^REDIS_PASSWORD=' "$ENV_FILE"; then
        sed -i "s/^REDIS_PASSWORD=.*/REDIS_PASSWORD=$REDIS_SECRET/" "$ENV_FILE"
    else
        printf '\nREDIS_PASSWORD=%s\n' "$REDIS_SECRET" >> "$ENV_FILE"
    fi
    sed -i "s/change_me_redis_password/$REDIS_SECRET/g" "$ENV_FILE"
fi

chmod 600 "$ENV_FILE"
# Which host user the containers hand ./storage to. Filled in rather than
# shipped as a value because a wrong number here is invisible until the storage
# directory stops belonging to the person running the script, and a number
# copied from an example file is wrong on every host whose user is not the
# first one created.
if ! grep -q '^PUID=' "$ENV_FILE"; then
    echo "PUID=$(id -u)" >> "$ENV_FILE"
fi
if ! grep -q '^PGID=' "$ENV_FILE"; then
    echo "PGID=$(id -g)" >> "$ENV_FILE"
fi
# A .env written before these were filled in can carry a uid that is not this
# user's, and the symptom is not a message about users: the storage directory
# simply stops belonging to the person running the script. Said here, where
# the cause is known.
CONFIGURED_PUID="$(sed -n 's/^PUID=//p' "$ENV_FILE" | tail -1)"
CONFIGURED_PGID="$(sed -n 's/^PGID=//p' "$ENV_FILE" | tail -1)"
if [ "$CONFIGURED_PUID" != "$(id -u)" ] || [ "$CONFIGURED_PGID" != "$(id -g)" ]; then
    note "WARNING: $ENV_FILE says PUID=$CONFIGURED_PUID PGID=$CONFIGURED_PGID,"
    note "         but you are $(id -un) (uid $(id -u), gid $(id -g))."
    note "         The containers hand ./storage to that uid, so it may not be yours to write."
    note "         The menu (Settings -> Storage ownership) or this fixes it:"
    note "           PUID=$(id -u)"
    note "           PGID=$(id -g)"
fi

# ── Display helpers ───────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_CYAN=$'\033[36m'; C_OFF=$'\033[0m'
else
    C_BOLD=""; C_DIM=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_CYAN=""; C_OFF=""
fi

is_secret_key() {
    case "$1" in
        *API_KEY|*PASSWORD|*TOKEN|*SECRET) return 0 ;;
    esac
    return 1
}

mask_value() {
    if [ ${#1} -gt 12 ]; then
        printf '%s...%s (%d chars)' "${1:0:4}" "${1: -4}" "${#1}"
    elif [ -n "$1" ]; then
        printf '****'
    else
        printf '(empty)'
    fi
}

confirm() {
    printf '%s' "$1"
    read -r ANSWER || ANSWER=""
    case "$ANSWER" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

pause_return() {
    printf '%s' "${C_DIM}Press Enter to return to the menu...${C_OFF}"
    read -r _PAUSE || true
    echo
}

# ── Feature defaults ──────────────────────────────────────────────────────────
# The interactive Features menu persists its toggles as FEATURE_* keys in .env
# so they survive the next run. Empty keeps the historical defaults; CLI flags
# below still override everything for a single run.
FEATURE_VAL="$(env_get FEATURE_BROWSER_RUNTIME)"; BROWSER_RUNTIME="${FEATURE_VAL:-1}"
FEATURE_VAL="$(env_get FEATURE_AGENT_RUNTIME)";     AGENT_RUNTIME="${FEATURE_VAL:-1}"
FEATURE_VAL="$(env_get FEATURE_VOICE)";             VOICE="${FEATURE_VAL:-0}"
FEATURE_VAL="$(env_get FEATURE_MIKU_LOCAL)";        MIKU_LOCAL="$FEATURE_VAL"
if [ -z "$MIKU_LOCAL" ] && grep -q '^MIKU_LLM_URL=http://miku-llm:' "$ENV_FILE"; then
    MIKU_LOCAL=1
fi

usage() {
    cat <<'USAGE'
Usage:
  ./start.sh                  Interactive menu (status, features, .env editor)
  ./start.sh up               Start immediately, no menu
  ./start.sh [PORT] | -p PORT Start on that port (updates .env)
  ./start.sh --down           Stop all containers
  ./start.sh --logs           Tail container logs
  ./start.sh --restart        Rebuild and recreate containers
  ./start.sh --chown-abort    Hand ./storage back to your user
  ./start.sh --no-browser-runtime | --browser-runtime
  ./start.sh --no-agent
  ./start.sh --miku-local | --no-miku-local
  ./start.sh --voice | --no-voice
USAGE
}

# Parse CLI arguments
PORT_ARG=""
ACTION="up"
RECREATE_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--port)
            PORT_ARG="$2"
            shift 2
            ;;
        up|start)
            ACTION="up"
            shift
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
        -h|--help)
            usage
            exit 0
            ;;
        *)
            if [[ "$1" =~ ^[0-9]+$ ]]; then
                PORT_ARG="$1"
                shift
            else
                echo "Unknown argument: $1"
                usage
                exit 1
            fi
            ;;
    esac
done

# ── Actions ───────────────────────────────────────────────────────────────────
# Both the CLI flags and the interactive menu call these, so there is exactly
# one copy of each docker compose invocation.

compose_files_args() {
    # The GPU overlay is opt-in: only mount /dev/dri when the host actually has
    # one, so the same stack boots on a headless server without it.
    COMPOSE_FILES=(-f docker-compose.yml)
    if [ "$MIKU_LOCAL" = "1" ] && [ -e /dev/dri ]; then
        COMPOSE_FILES+=(-f docker-compose.gpu.yml)
        export MIKU_LLM_THREADS="${MIKU_LLM_THREADS:-$(nproc 2>/dev/null || echo 4)}"
        export MIKU_LLM_CPUS="${MIKU_LLM_CPUS:-$(nproc 2>/dev/null || echo 4)}"
        echo " GPU: detected /dev/dri, model will be served with acceleration"
    elif [ "$MIKU_LOCAL" = "1" ]; then
        export MIKU_LLM_THREADS="${MIKU_LLM_THREADS:-$(nproc 2>/dev/null || echo 4)}"
        export MIKU_LLM_CPUS="${MIKU_LLM_CPUS:-$(nproc 2>/dev/null || echo 4)}"
        echo " GPU: none found, model will be served on CPU"
    fi
}

do_down() {
    echo "Stopping NetSanctum containers..."
    docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile browser --profile agent --profile miku-local --profile voice down --remove-orphans 2>/dev/null \
        || docker compose --profile browser --profile agent --profile miku-local --profile voice down --remove-orphans
    echo "NetSanctum stopped."
}

do_logs() {
    docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile browser --profile agent --profile miku-local --profile voice logs -f --tail=100 2>/dev/null \
        || docker compose --profile browser --profile agent --profile miku-local --profile voice logs -f --tail=100
}

do_chown_abort() {
    # A repair, not a start. The storage directory is handed to a uid read from
    # .env and then locked to that uid alone, so a host whose user is not that
    # number - or one that has since changed - is left unable to read its own
    # data with no way back in short of root. The fix is the one the stack
    # already has the means to make, run on its own: a container started as
    # root, changing the ownership the containers themselves would have set.
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
    if ! docker run --rm --user 0:0 \
        -e TARGET_UID="$(id -u)" -e TARGET_GID="$(id -g)" \
        -v "$ABS_DIR:/target" \
        "$REPAIR_IMAGE" sh -ec 'chown -R "$TARGET_UID:$TARGET_GID" /target && chmod -R a+rwX /target'
    then
        echo "Error: could not change $TARGET_DIR. Check that the docker daemon is running."
        return 1
    fi
    echo "Done. $(ls -ld "$ABS_DIR" | awk '{print $1, $3":"$4}')"
}

# Said here rather than left to mkdir, which reports "Permission denied"
# without saying which directory, which user, or what to do about it - and the
# voice profile cannot start without these weights, so the message is the whole
# diagnosis.
voice_model_dir_unusable() {
    echo "Error: $VOICE_MODEL_DIR is not usable by the user running this script."
    echo "  user:   $(id -un) (uid $(id -u))"
    echo "  path:   $VOICE_MODEL_DIR"
    if [ -L "./storage" ]; then
        # The shape that reports the parent rather than the child: storage is a
        # symlink, so the refusal is about wherever it points, and it is
        # invisible in the path above.
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
    return 1
}

do_up() {
    # If a port was specified on the CLI, update HOST_PORT in .env
    if [ -n "$PORT_ARG" ]; then
        if ! [[ "$PORT_ARG" =~ ^[0-9]+$ ]] || [ "$PORT_ARG" -lt 1 ] || [ "$PORT_ARG" -gt 65535 ]; then
            echo "Error: '$PORT_ARG' is not a valid port (1-65535)."
            exit 1
        fi
        env_set HOST_PORT "$PORT_ARG"
        echo "Updated HOST_PORT=$PORT_ARG in $ENV_FILE"
    fi
    HOST_PORT="$(env_get HOST_PORT | tr -d ' "'$'\r')"
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

    VOICE_MODEL_DIR="${MIKU_VOICE_MODEL_DIR:-./storage/voice-models}"
    if [ "$VOICE" = "1" ]; then
        if [ ! -d "$VOICE_MODEL_DIR" ] && ! mkdir -p "$VOICE_MODEL_DIR" 2>/dev/null; then
            voice_model_dir_unusable || return 1
        elif [ -d "$VOICE_MODEL_DIR" ] && [ ! -w "$VOICE_MODEL_DIR" ]; then
            # The directory is there but belongs to someone else, which is what
            # a stack first started with sudo leaves behind. Creating it would
            # have succeeded, so this case is the one a bare mkdir never
            # reports.
            voice_model_dir_unusable || return 1
        fi
    fi
    if [ "$MIKU_LOCAL" = "1" ]; then
        MODEL_FILE="$(sed -n 's/^MIKU_MODEL_FILE=//p' "$ENV_FILE" | tail -1)"
        MODEL_URL="$(sed -n 's/^MIKU_MODEL_URL=//p' "$ENV_FILE" | tail -1)"
        MODEL_DIR="$(sed -n 's/^MIKU_MODEL_DIR=//p' "$ENV_FILE" | tail -1)"
        MODEL_DIR="${MODEL_DIR:-./storage/models}"
        if [ -z "$MODEL_FILE" ] || [ -z "$MODEL_URL" ]; then
            echo "Error: the local model needs both MIKU_MODEL_FILE and MIKU_MODEL_URL in .env"
            echo "  Either fill them in, or place the weights at $MODEL_DIR/$MODEL_FILE yourself."
            return 1
        fi
        if [ ! -s "$MODEL_DIR/$MODEL_FILE" ]; then
            AVAILABLE="$(df -Pk "$MODEL_DIR" 2>/dev/null | awk 'NR==2 {print int($4/1024/1024)}')"
            if [ -n "$AVAILABLE" ] && [ "$AVAILABLE" -lt 2 ]; then
                echo "Error: $MODEL_DIR has less than 2 GB free, which is not enough for the model"
                return 1
            fi
        fi
    fi

    echo "Building and launching Docker services..."
    compose_files_args
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
        # Stopped and removed, not just left unused: the point of the browser
        # mode is that the server stops holding a speech model in memory.
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
}

# ── Interactive menu ──────────────────────────────────────────────────────────
# Plain bash: reads numbers, redraws with clear. No whiptail/dialog dependency,
# works over ssh, and stays readable in a scrollback.

COMPOSE_PROJECT="$(basename "$PROJECT_DIR")"
COMPOSE_PROJECT="${COMPOSE_PROJECT,,}"
COMPOSE_PROJECT="${COMPOSE_PROJECT//[^a-z0-9_-]/}"

running_services() {
    docker ps --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
        --format '{{.Label "com.docker.compose.service"}}' 2>/dev/null | sort || true
}

stack_status_line() {
    if ! command -v docker >/dev/null 2>&1; then
        printf '%s' "${C_RED}docker not found${C_OFF}"
        return
    fi
    RUNNING="$(running_services)"
    if [ -n "$RUNNING" ]; then
        COUNT="$(printf '%s\n' "$RUNNING" | wc -l)"
        NAMES="$(printf '%s\n' "$RUNNING" | head -8 | paste -sd' ' -)"
        printf '%s' "${C_GREEN}running${C_OFF} (${COUNT} services): ${C_DIM}${NAMES}${C_OFF}"
    else
        printf '%s' "${C_DIM}not running${C_OFF}"
    fi
}

ENV_BACKED_UP=0
backup_env_once() {
    # One rolling snapshot per script run, taken before the first menu edit:
    # cheap insurance for a file that holds generated secrets no one can
    # recover from anywhere else.
    if [ "$ENV_BACKED_UP" = "0" ]; then
        cp -p "$ENV_FILE" "${ENV_FILE}.bak"
        ENV_BACKED_UP=1
        printf '%s\n' "${C_DIM}(previous .env saved as ${ENV_FILE}.bak)${C_OFF}"
    fi
}

validate_value() {
    case "$1" in
        HOST_PORT)
            if ! [[ "$2" =~ ^[0-9]+$ ]] || [ "$2" -lt 1 ] || [ "$2" -gt 65535 ]; then
                echo "$1 must be a port number (1-65535)."
                return 1
            fi
            ;;
        SECURE_COOKIES|ALLOW_REMOTE_METADATA_FETCH)
            case "$2" in
                true|false) ;;
                *) echo "$1 must be true or false."; return 1 ;;
            esac
            ;;
        NETSANCTUM_HSTS_INCLUDE_SUBDOMAINS|AGENT_LLM_THINKING|VOICE_EDGE|VOICE_TIMBRE|VOICE_FETCH_TIMBRE)
            case "$2" in
                0|1) ;;
                *) echo "$1 must be 0 or 1."; return 1 ;;
            esac
            ;;
        VOICE_MAX_RESIDENT)
            case "$2" in
                1|2) ;;
                *) echo "$1 must be 1 or 2."; return 1 ;;
            esac
            ;;
        MIKU_LLM_CTX|MIKU_LLM_THREADS)
            if ! [[ "$2" =~ ^[0-9]+$ ]]; then
                echo "$1 must be a number."
                return 1
            fi
            ;;
        STORAGE_BACKEND)
            case "$2" in
                local|s3) ;;
                *) echo "$1 must be local or s3."; return 1 ;;
            esac
            ;;
    esac
    return 0
}

edit_key() {
    local KEY="$1" HINT="$2" CUR NEW
    CUR="$(env_get "$KEY")"
    printf '%s\n' "${C_BOLD}${KEY}${C_OFF} — ${HINT}"
    if is_secret_key "$KEY"; then
        printf '  current: %s\n' "$(mask_value "$CUR")"
    else
        printf '  current: %s\n' "${CUR:-(empty)}"
    fi
    printf '%s' "  new value (Enter keeps, '-' clears): "
    read -r NEW || NEW=""
    if [ -z "$NEW" ]; then
        return 0
    fi
    [ "$NEW" = "-" ] && NEW=""
    if ! ERR="$(validate_value "$KEY" "$NEW")"; then
        printf '%s\n' "${C_RED}  ${ERR}${C_OFF}"
        printf '%s' "  Press Enter to continue..."
        read -r _ || true
        echo
        return 0
    fi
    backup_env_once
    env_set "$KEY" "$NEW"
    if is_secret_key "$KEY" && [ -n "$NEW" ]; then
        printf '  saved: %s=%s\n' "$KEY" "$(mask_value "$NEW")"
    else
        printf '  saved: %s=%s\n' "$KEY" "${NEW:-(empty)}"
    fi
    sleep 0.4
}

# A curated, grouped view of .env: the knobs an operator actually turns. Keys
# outside these lists are never touched by the menu and stay editable by hand.
SECTION_KEYS=()
SECTION_HINTS=()
load_section() {
    SECTION_KEYS=()
    SECTION_HINTS=()
    case "$1" in
        web)
            SECTION_KEYS=(HOST_PORT TRUSTED_HOSTS PUBLIC_BASE_URL SECURE_COOKIES NETSANCTUM_TRUSTED_PROXY_IPS NETSANCTUM_HSTS_INCLUDE_SUBDOMAINS ALLOW_REMOTE_METADATA_FETCH NETSANCTUM_EXTENSION_ORIGIN_IDS)
            SECTION_HINTS=("host port the web UI is published on"
                "comma-separated Host values the app answers to"
                "public https:// URL when behind a reverse proxy"
                "true/false — set the Secure cookie flag"
                "proxies whose X-Forwarded-* headers are trusted"
                "0/1 — HSTS covers every subdomain of this name"
                "true/false — let the server fetch remote metadata"
                "extension IDs allowed to sign in and capture")
            ;;
        modules)
            SECTION_KEYS=(NETSANCTUM_MODULES NETSANCTUM_EXTERNAL_MODULES ENABLED_MODULES BROWSER_EGRESS_HOSTS)
            SECTION_HINTS=("modules baked into the image ('core' or a comma list)"
                "external module ids with matching pyproject extras"
                "optional module allowlist; empty enables all installed"
                "union of hosts browser-enabled modules may request")
            ;;
        assistant)
            SECTION_KEYS=(MIKU_LLM_URL MIKU_LLM_MODEL MIKU_LLM_API_KEY MIKU_STT_URL MIKU_STT_MODEL MIKU_STT_API_KEY MIKU_TTS_URL MIKU_TTS_MODEL MIKU_TTS_API_KEY AGENT_LLM_THINKING)
            SECTION_HINTS=("OpenAI-compatible chat endpoint the agent calls"
                "model name at the chat endpoint"
                "API key for the chat endpoint"
                "audio transcription endpoint (empty = browser STT)"
                "model name at the transcription endpoint"
                "API key for the transcription endpoint"
                "speech synthesis endpoint (empty = browser TTS)"
                "model name at the synthesis endpoint"
                "API key for the synthesis endpoint"
                "0/1 — hidden thinking; small local models need 0")
            ;;
        local_model)
            SECTION_KEYS=(MIKU_MODEL_FILE MIKU_MODEL_URL MIKU_LLM_CTX MIKU_LLM_THREADS)
            SECTION_HINTS=("GGUF file name to serve"
                "where the stack downloads the weights on first run"
                "context window size for the local model"
                "CPU threads (raised to the host core count at start)")
            ;;
        voice)
            SECTION_KEYS=(VOICE_EDGE VOICE_EDGE_VOICE_RU VOICE_EDGE_VOICE_EN VOICE_MAX_RESIDENT VOICE_TIMBRE)
            SECTION_HINTS=("0/1 — online neural voice; spoken text leaves this machine"
                "neural voice name for Russian replies"
                "neural voice name for English replies"
                "1 or 2 engines kept resident in memory"
                "0/1 — voice conversion on top of the engine (slow)")
            ;;
        storage)
            SECTION_KEYS=(STORAGE_BACKEND LOCAL_STORAGE_ROOT)
            SECTION_HINTS=("local or s3"
                "root directory for local storage")
            ;;
    esac
}

section_menu() { # id title
    local ID="$1" TITLE="$2" CHOICE IDX KEY DISPLAY
    load_section "$ID"
    if [ ${#SECTION_KEYS[@]} -eq 0 ]; then
        return 0
    fi
    while true; do
        clear >/dev/null 2>&1 || true
        printf '%s\n' "${C_BOLD}NetSanctum — .env: ${TITLE}${C_OFF}"
        printf '%s\n' "${C_DIM}Editing ${ENV_FILE}. Enter keeps the current value.${C_OFF}"
        echo
        IDX=1
        for KEY in "${SECTION_KEYS[@]}"; do
            DISPLAY="$(env_get "$KEY")"
            if is_secret_key "$KEY"; then
                DISPLAY="$(mask_value "$DISPLAY")"
            fi
            if [ ${#DISPLAY} -gt 46 ]; then
                DISPLAY="${DISPLAY:0:43}..."
            fi
            printf '  %2d) %-36s %s\n' "$IDX" "$KEY" "=${DISPLAY:-(empty)}"
            IDX=$((IDX + 1))
        done
        printf '%s\n' "   b) Back"
        printf '%s' "Select: "
        read -r CHOICE || CHOICE=""
        case "$CHOICE" in
            b|B|q|Q) return 0 ;;
            *)
                if [[ "$CHOICE" =~ ^[0-9]+$ ]] && [ "$CHOICE" -ge 1 ] && [ "$CHOICE" -le ${#SECTION_KEYS[@]} ]; then
                    IDX=$((CHOICE - 1))
                    echo
                    edit_key "${SECTION_KEYS[$IDX]}" "${SECTION_HINTS[$IDX]}"
                fi
                ;;
        esac
    done
}

rotate_postgres_password() {
    local OLD NEW PG_USER PG_DB
    OLD="$(env_get POSTGRES_PASSWORD)"
    NEW="$(gen_secret)"
    backup_env_once
    env_set POSTGRES_PASSWORD "$NEW"
    replace_all_literal ":${OLD}@" ":${NEW}@"
    printf '%s\n' "  POSTGRES_PASSWORD updated (the DATABASE_URLs follow it)."
    # POSTGRES_PASSWORD only seeds a fresh volume: a postgres that already
    # initialized keeps its old password and would refuse every connection
    # with the new one. Change it in the database too, while it is reachable;
    # inside the container no password is needed to do that.
    PG_USER="$(env_get POSTGRES_USER)"
    PG_DB="$(env_get POSTGRES_DB)"
    if docker compose -f docker-compose.yml exec -T postgres \
        psql -U "$PG_USER" -d "$PG_DB" -c "ALTER USER \"${PG_USER}\" WITH PASSWORD '${NEW}';" >/dev/null 2>&1; then
        printf '%s\n' "  The running postgres was updated too; restart applies everything."
    else
        printf '%s\n' "${C_YELLOW}  Postgres is not reachable, so the database still knows the old password.${C_OFF}"
        printf '%s\n' "${C_YELLOW}  On a fresh volume the new one is used anyway; otherwise run:${C_OFF}"
        printf '%s\n' "${C_YELLOW}    docker compose exec postgres psql -U ${PG_USER} -d ${PG_DB} -c \"ALTER USER \\\"${PG_USER}\\\" WITH PASSWORD '<new>';\"${C_OFF}"
        printf '%s\n' "${C_YELLOW}  (the generated password is in ${ENV_FILE})${C_OFF}"
    fi
}

rotate_redis_password() {
    local OLD NEW
    OLD="$(env_get REDIS_PASSWORD)"
    NEW="$(gen_secret)"
    backup_env_once
    env_set REDIS_PASSWORD "$NEW"
    replace_all_literal ":${OLD}@" ":${NEW}@"
    printf '%s\n' "  REDIS_PASSWORD updated (broker and state URLs follow it)."
    printf '%s\n' "  Redis reads the password at container start — Restart applies it."
}

rotate_master_key() {
    local NEW
    backup_env_once
    NEW="$(gen_secret)"
    env_set MASTER_API_KEY "$NEW"
    printf '  new %s\n' "${C_BOLD}MASTER_API_KEY=$NEW${C_OFF}"
    printf '%s\n' "  Copy it now; it is shown once and needed by external integrations."
    printf '%s\n' "  Restart applies it."
}

rotate_agent_tokens() {
    backup_env_once
    env_set AGENT_RUNTIME_TOKEN "$(gen_secret)"
    env_set AGENT_INTERNAL_KEY "$(gen_secret)"
    printf '%s\n' "  AGENT_RUNTIME_TOKEN and AGENT_INTERNAL_KEY regenerated. Restart applies them."
}

secrets_menu() {
    local CHOICE
    while true; do
        clear >/dev/null 2>&1 || true
        printf '%s\n' "${C_BOLD}NetSanctum — .env: Secrets${C_OFF}"
        printf '%s\n' "${C_DIM}Values are generated, not typed: rotating a password rewrites the
URLs that embed it. FILE_ENCRYPTION_KEY is absent on purpose — compose keeps
the active key in a volume and rotation would orphan old ciphertext.${C_OFF}"
        echo
        printf '%s\n' "  1) Regenerate PostgreSQL password   (DATABASE_URL, DATABASE_URL_SYNC follow)"
        printf '%s\n' "  2) Regenerate Redis password        (broker and vault-state URLs follow)"
        printf '%s\n' "  3) Regenerate MASTER_API_KEY        (printed once)"
        printf '%s\n' "  4) Regenerate agent tokens          (AGENT_RUNTIME_TOKEN, AGENT_INTERNAL_KEY)"
        printf '%s\n' "  5) Show MASTER_API_KEY"
        printf '%s\n' "  b) Back"
        printf '%s' "Select: "
        read -r CHOICE || CHOICE=""
        case "$CHOICE" in
            1) echo; rotate_postgres_password; pause_return ;;
            2) echo; rotate_redis_password; pause_return ;;
            3) echo; rotate_master_key; pause_return ;;
            4) echo; rotate_agent_tokens; pause_return ;;
            5)
                echo
                printf '  MASTER_API_KEY=%s\n' "$(env_get MASTER_API_KEY)"
                pause_return
                ;;
            b|B|q|Q) return 0 ;;
        esac
    done
}

ownership_menu() {
    local CHOICE
    while true; do
        clear >/dev/null 2>&1 || true
        printf '%s\n' "${C_BOLD}NetSanctum — .env: Storage ownership${C_OFF}"
        echo
        printf '  .env:      PUID=%s PGID=%s\n' "$(env_get PUID)" "$(env_get PGID)"
        printf '  you are:   %s (uid %s, gid %s)\n' "$(id -un)" "$(id -u)" "$(id -g)"
        if [ "$(env_get PUID)" = "$(id -u)" ] && [ "$(env_get PGID)" = "$(id -g)" ]; then
            printf '    %s\n' "${C_GREEN}match${C_OFF}"
        else
            printf '  %s\n' "${C_RED}  mismatch: ./storage is handed to that uid, not to you${C_OFF}"
        fi
        echo
        printf '%s\n' "  1) Fix PUID/PGID to my user"
        printf '%s\n' "  b) Back"
        printf '%s' "Select: "
        read -r CHOICE || CHOICE=""
        case "$CHOICE" in
            1)
                backup_env_once
                env_set PUID "$(id -u)"
                env_set PGID "$(id -g)"
                printf '%s\n' "  Saved. The containers chown ./storage to it on the next Start."
                pause_return
                ;;
            b|B|q|Q) return 0 ;;
        esac
    done
}

# ── Module enablement ─────────────────────────────────────────────────────────
# ENABLED_MODULES is an allowlist: empty means every installed optional module
# runs (and the dashboard's own persisted state adds nothing), while a non-empty
# value is the exact allowlist and overrides the dashboard. The menu therefore
# always works from an explicit list: the first toggle materializes "all
# installed", then flips single entries, so the confusing empty state never
# has to be read or written by hand.

installed_optional_modules() {
    local REQUESTED
    REQUESTED="$(env_get NETSANCTUM_MODULES)"
    REQUESTED="${REQUESTED:-default}"
    if [ "$REQUESTED" = "default" ] && [ -f module-build.json ]; then
        # The same expansion module_build.py performs for "default": the
        # committed catalog's default_modules. Required modules are left out —
        # they cannot be disabled.
        sed -n '/"default_modules": \[/,/\]/p' module-build.json \
            | grep -o '"[a-z0-9_]*"' | tr -d '"' | grep -vx default_modules | sort || true
    else
        printf '%s\n' "${REQUESTED//,/$'\n'}" | sed 's/[[:space:]]//g' | sed '/^$/d' | sort
    fi
}

module_enabled() {
    local ENABLED
    ENABLED="$(env_get ENABLED_MODULES)"
    if [ -z "$ENABLED" ]; then
        return 0
    fi
    case ",$ENABLED," in
        *",$1,"*) return 0 ;;
    esac
    return 1
}

toggle_module() {
    local MOD="$1" ENABLED NEWLIST
    ENABLED="$(env_get ENABLED_MODULES)"
    if [ -z "$ENABLED" ]; then
        NEWLIST="$(installed_optional_modules | grep -vx "$MOD" | paste -sd, -)"
    elif module_enabled "$MOD"; then
        NEWLIST="$(printf '%s\n' "$ENABLED" | tr ',' '\n' | grep -vx "$MOD" | paste -sd, -)"
    else
        NEWLIST="${ENABLED},${MOD}"
    fi
    if [ -z "$NEWLIST" ]; then
        printf '%s\n' "${C_YELLOW}  Cannot disable the last enabled module: an empty ENABLED_MODULES${C_OFF}"
        printf '%s\n' "${C_YELLOW}  would re-enable everything. Keep at least one enabled.${C_OFF}"
        pause_return
        return 0
    fi
    backup_env_once
    env_set ENABLED_MODULES "$NEWLIST"
}

modules_menu() {
    local CHOICE MODULES IDX MOD RAW_ENABLED
    while true; do
        clear >/dev/null 2>&1 || true
        mapfile -t MODULES < <(installed_optional_modules)
        RAW_ENABLED="$(env_get ENABLED_MODULES)"
        printf '%s\n' "${C_BOLD}NetSanctum — Modules${C_OFF}"
        if [ -z "$RAW_ENABLED" ]; then
            printf '%s\n' "${C_DIM}ENABLED_MODULES is empty: every installed module below runs. The first
toggle writes an explicit list (which then overrides the dashboard's switches).
Restart applies changes.${C_OFF}"
        else
            printf '%s\n' "${C_DIM}ENABLED_MODULES is the exact allowlist of what may run. Restart applies
changes.${C_OFF}"
        fi
        echo
        printf '%s\n' "${C_DIM}  always on: auth, settings, sharing${C_OFF}"
        if [ ${#MODULES[@]} -eq 0 ]; then
            printf '%s\n' "${C_YELLOW}  no optional modules resolved from NETSANCTUM_MODULES${C_OFF}"
        fi
        IDX=1
        for MOD in "${MODULES[@]}"; do
            if module_enabled "$MOD"; then
                printf '  %2d) %s[x]%s %s\n' "$IDX" "$C_GREEN" "$C_OFF" "$MOD"
            else
                printf '  %2d) %s[ ]%s %s\n' "$IDX" "$C_DIM" "$C_OFF" "$MOD"
            fi
            IDX=$((IDX + 1))
        done
        echo
        printf '%s\n' "   v) Advanced values   NETSANCTUM_MODULES (what the image is built with),"
        printf '%s\n' "                         external modules, browser egress hosts"
        printf '%s\n' "   b) Back"
        printf '%s' "Select a number to toggle: "
        read -r CHOICE || CHOICE=""
        case "$CHOICE" in
            v|V) section_menu modules "Modules — advanced values" ;;
            b|B|q|Q) return 0 ;;
            *)
                if [[ "$CHOICE" =~ ^[0-9]+$ ]] && [ "$CHOICE" -ge 1 ] && [ "$CHOICE" -le ${#MODULES[@]} ]; then
                    toggle_module "${MODULES[$((CHOICE - 1))]}"
                fi
                ;;
        esac
    done
}

settings_menu() {
    local CHOICE
    while true; do
        clear >/dev/null 2>&1 || true
        printf '%s\n' "${C_BOLD}NetSanctum — .env${C_OFF}"
        printf '%s\n' "${C_DIM}A curated editor: comments, order and keys not listed here are left
alone in ${ENV_FILE}. Every change is one line; a .bak snapshot is kept.${C_OFF}"
        echo
        printf '%s\n' "  1) Web & access        port, hosts, proxy trust, cookies"
        printf '%s\n' "  2) Modules             enable/disable installed modules"
        printf '%s\n' "  3) Assistant providers chat/STT/TTS endpoints and keys"
        printf '%s\n' "  4) Local model         GGUF weights and sizing"
        printf '%s\n' "  5) Voice               engines and voices"
        printf '%s\n' "  6) Storage             backend and root"
        printf '%s\n' "  7) Secrets             regenerate passwords and keys"
        printf '%s\n' "  8) Storage ownership   PUID/PGID"
        printf '%s\n' "  b) Back"
        printf '%s' "Select: "
        read -r CHOICE || CHOICE=""
        case "$CHOICE" in
            1) section_menu web "Web & access" ;;
            2) modules_menu ;;
            3) section_menu assistant "Assistant providers" ;;
            4) section_menu local_model "Local model" ;;
            5) section_menu voice "Voice" ;;
            6) section_menu storage "Storage" ;;
            7) secrets_menu ;;
            8) ownership_menu ;;
            b|B|q|Q) return 0 ;;
        esac
    done
}

feature_row() { # number state label description
    if [ "$2" = "1" ]; then
        printf '  %s) %s[x]%s %-18s %s\n' "$1" "$C_GREEN" "$C_OFF" "$3" "$4"
    else
        printf '  %s) %s[ ]%s %-18s %s\n' "$1" "$C_DIM" "$C_OFF" "$3" "$4"
    fi
}

features_menu() {
    local CHOICE
    while true; do
        clear >/dev/null 2>&1 || true
        printf '%s\n' "${C_BOLD}NetSanctum — Features${C_OFF}"
        printf '%s\n' "${C_DIM}Toggles are written to .env as FEATURE_* and used as the defaults
on every later run; CLI flags still override them once. Turning an item off
stops and removes its containers on the next Start.${C_OFF}"
        echo
        feature_row 1 "$BROWSER_RUNTIME" "Browser runtime" "containers browser-runtime, browser-proxy"
        feature_row 2 "$AGENT_RUNTIME"   "Agent runtime"   "container agent-runtime (assistant engine)"
        feature_row 3 "$MIKU_LOCAL"      "Local model"     "containers miku-llm, model-init (llama.cpp)"
        feature_row 4 "$VOICE"           "Voice"           "containers miku-voice, miku-stt, voice-init"
        if [ "$MIKU_LOCAL" = "1" ]; then
            MODEL_FILE="$(env_get MIKU_MODEL_FILE)"
            MODEL_URL="$(env_get MIKU_MODEL_URL)"
            if [ -z "$MODEL_FILE" ] || [ -z "$MODEL_URL" ]; then
                printf '  %s note: MIKU_MODEL_FILE/MIKU_MODEL_URL are empty — Start will refuse\n' "$C_YELLOW"
                printf '  until they are set in Settings -> Local model.%s\n' "$C_OFF"
            fi
            case "$(env_get MIKU_LLM_URL)" in
                ""|http://miku-llm:*) ;;
                *)
                    printf '  %s note: MIKU_LLM_URL points elsewhere, so the agent will not use\n' "$C_YELLOW"
                    printf '  the local model until it is cleared or pointed at the service.%s\n' "$C_OFF"
                    ;;
            esac
        fi
        echo
        printf '%s\n' "  b) Back"
        printf '%s' "Select a number to toggle: "
        read -r CHOICE || CHOICE=""
        case "$CHOICE" in
            1)
                if [ "$BROWSER_RUNTIME" = "1" ]; then BROWSER_RUNTIME=0; else BROWSER_RUNTIME=1; fi
                backup_env_once
                env_set FEATURE_BROWSER_RUNTIME "$BROWSER_RUNTIME"
                ;;
            2)
                if [ "$AGENT_RUNTIME" = "1" ]; then AGENT_RUNTIME=0; else AGENT_RUNTIME=1; fi
                backup_env_once
                env_set FEATURE_AGENT_RUNTIME "$AGENT_RUNTIME"
                ;;
            3)
                if [ "$MIKU_LOCAL" = "1" ]; then MIKU_LOCAL=0; else MIKU_LOCAL=1; fi
                backup_env_once
                env_set FEATURE_MIKU_LOCAL "$MIKU_LOCAL"
                ;;
            4)
                if [ "$VOICE" = "1" ]; then VOICE=0; else VOICE=1; fi
                backup_env_once
                env_set FEATURE_VOICE "$VOICE"
                ;;
            b|B|q|Q) return 0 ;;
        esac
    done
}

open_web_ui() {
    HOST_PORT="$(env_get HOST_PORT | tr -d ' "'$'\r')"
    HOST_PORT="${HOST_PORT:-8000}"
    local URL="http://localhost:${HOST_PORT}"
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1 || echo "Could not open a browser; visit $URL"
    else
        echo "Visit $URL"
    fi
}

main_menu() {
    local CHOICE
    while true; do
        clear >/dev/null 2>&1 || true
        printf '%s\n' "========================================================"
        printf ' %s\n' "${C_BOLD}NetSanctum${C_OFF} — $(stack_status_line)"
        printf ' port %s' "$(env_get HOST_PORT)"
        [ -e /dev/dri ] && printf '%s' " · GPU: /dev/dri"
        echo
        printf '%s\n' "========================================================"
        if [ -n "$ENV_NOTICES" ]; then
            printf '%s' "${C_YELLOW}${ENV_NOTICES}${C_OFF}"
        fi
        echo
        printf '%s\n' "  1) Start            build and launch the stack"
        printf '%s\n' "  2) Restart          rebuild and recreate containers"
        printf '%s\n' "  3) Stop             stop and remove containers"
        printf '%s\n' "  4) Logs             follow container logs (Ctrl-C to return)"
        printf '%s\n' "  5) Features         toggle browser/AI/voice containers"
        printf '%s\n' "  6) Settings         edit .env values and secrets"
        printf '%s\n' "  7) Repair storage   hand ./storage back to your user"
        printf '%s\n' "  8) Open web UI      http://localhost:$(env_get HOST_PORT)"
        printf '%s\n' "  q) Quit"
        echo
        printf '%s' "Select: "
        read -r CHOICE || CHOICE=""
        case "$CHOICE" in
            1) do_up || true; pause_return ;;
            2) ACTION="restart"; do_up || true; ACTION="up"; pause_return ;;
            3) do_down || true; pause_return ;;
            4) do_logs || true; pause_return ;;
            5) features_menu ;;
            6) settings_menu ;;
            7)
                if confirm "Hand ./storage back to $(id -un) and reopen it to every local user? [y/N] "; then
                    echo
                    do_chown_abort || true
                fi
                pause_return
                ;;
            8) open_web_ui; pause_return ;;
            q|Q) exit 0 ;;
            *) ;;
        esac
    done
}

# ── Dispatch ──────────────────────────────────────────────────────────────────

if [ $# -eq 0 ]; then
    if [ ! -t 0 ]; then
        echo "No arguments and stdin is not a terminal — nothing to do."
        usage
        exit 1
    fi
    main_menu
    exit 0
fi

if [ "$ACTION" = "down" ]; then
    do_down
    exit 0
fi

if [ "$ACTION" = "logs" ]; then
    do_logs
    exit 0
fi

if [ "$ACTION" = "chown-abort" ]; then
    do_chown_abort
    exit 0
fi

do_up
