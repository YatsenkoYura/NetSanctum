#!/bin/sh
# Fetch model files into the models directory, then get out of the way.
#
# Runs in a bare Alpine container, so the only tools available are BusyBox ones.
# BusyBox wget understands a small option set and rejects anything else before it
# opens a connection, which is why this script sticks to -c, -O and -S and uses no
# long options. A rejected flag looks exactly like a failed download otherwise.
#
# Two modes:
#   single    MODEL_FILE + MODEL_URL (+ optional MODEL_MAGIC)
#   manifest  --manifest, reading VOICE_MANIFEST as lines of name|url|magic
#
# The magic check is the point of the exercise: a proxy or an error page can answer
# 200 with something that is not a model, and installing that produces a service
# that fails much later, somewhere less obvious.
set -eu

MODELS_DIR="${MODELS_DIR:-/models}"
TARGET_UID="${TARGET_UID:-0}"
TARGET_GID="${TARGET_GID:-0}"

# What the first bytes of a real file look like. whisper.cpp still ships its models
# in the older ggml container, whose magic is "lmgg" rather than "GGUF", so both are
# recognised. TorchScript and zip both start with "PK", and an ONNX model is a
# protobuf whose first field is the IR version.
magic_matches() {
    file="$1"
    expected="$2"
    case "$expected" in
        ""|none|NONE) return 0 ;;
        GGUF) [ "$(head -c 4 "$file")" = "GGUF" ] ;;
        GGML) [ "$(head -c 4 "$file")" = "lmgg" ] ;;
        ONNX) [ "$(head -c 1 "$file" | od -An -tu1 | tr -d ' ')" = "8" ] ;;
        ZIP|PK|PT) [ "$(head -c 2 "$file")" = "PK" ] ;;
        # A JSON sidecar: the first non-whitespace byte decides, so a proxy's error
        # page is caught the same way an error page in place of a model is.
        JSON|json) head -c 64 "$file" | grep -qE '^[[:space:]]*[{\[]' ;;
        *) echo "Error: unknown magic '$expected' for $file" >&2; return 1 ;;
    esac
}

fetch_one() {
    name="$1"
    url="$2"
    magic="${3:-GGUF}"
    target="$MODELS_DIR/$name"
    part="$target.part"
    # A manifest entry may name a file inside a directory - the content model is three
    # files in one - and the directory is not something the operator has to create
    # before the fetcher can fill it.
    mkdir -p "$(dirname "$target")"

    if [ -s "$target" ]; then
        echo "model already present: $name"
        return 0
    fi
    echo "downloading $name from $url"
    # -c resumes a partial transfer, -O names the file, -S shows the server response
    # so a failure explains itself. No -q: a silent failure is indistinguishable
    # from a blocked network until somebody reads these logs.
    if ! wget -c -S -O "$part" "$url"; then
        echo "Error: the download of $name failed." >&2
        echo "  url:    $url" >&2
        echo "  target: $target" >&2
        echo "  check that this host can reach that url and has room for the file" >&2
        rm -f "$part"
        return 1
    fi
    if ! magic_matches "$part" "$magic"; then
        echo "Error: what was downloaded for $name is not a $magic file." >&2
        echo "  first bytes: $(head -c 4 "$part" | od -c | head -1)" >&2
        rm -f "$part"
        return 1
    fi
    mv "$part" "$target"
    return 0
}

finish() {
    # A bind mount the daemon cannot chown must not throw away a good download.
    for path in "$@"; do
        [ -e "$path" ] || continue
        chown "$TARGET_UID:$TARGET_GID" "$path" 2>/dev/null ||
            echo "warning: could not chown $path to $TARGET_UID:$TARGET_GID, continuing"
        chmod 0644 "$path" 2>/dev/null ||
            echo "warning: could not chmod $path, continuing"
        echo "model ready: ${path##*/} ($(du -h "$path" | cut -f1))"
    done
}

if [ "${1:-}" = "--manifest" ]; then
    if [ -z "${VOICE_MANIFEST:-}" ]; then
        echo "VOICE_MANIFEST must list name|url|magic lines when --manifest is used" >&2
        exit 1
    fi
    fetched=""
    # Fed from a here-doc rather than a pipe on purpose: a loop fed by a pipe runs in
    # a subshell, so its failure would exit that subshell and the script would carry
    # on to report success with models missing.
    while IFS='|' read -r name url magic; do
        [ -n "${name:-}" ] || continue
        case "$name" in \#*) continue ;; esac
        fetch_one "$name" "$url" "${magic:-GGUF}"
        fetched="$fetched $MODELS_DIR/$name"
    done <<MANIFEST
$VOICE_MANIFEST
MANIFEST
    # A missing model is fatal here, not later: a service that starts without one
    # fails far from the cause.
    # shellcheck disable=SC2086
    finish $fetched
    exit 0
fi

if [ -z "${MODEL_FILE:-}" ] || [ -z "${MODEL_URL:-}" ]; then
    echo "MIKU_MODEL_FILE and MIKU_MODEL_URL must both be set to fetch a model" >&2
    exit 1
fi
fetch_one "$MODEL_FILE" "$MODEL_URL" "${MODEL_MAGIC:-GGUF}"
finish "$MODELS_DIR/$MODEL_FILE"
