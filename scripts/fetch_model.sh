#!/bin/sh
# Fetch one model file into the models directory, then get out of the way.
#
# Runs in a bare Alpine container, so the only tools available are BusyBox ones.
# BusyBox wget understands a small option set and rejects anything else before it
# opens a connection, which is why this script sticks to -c, -O and -S and uses no
# long options. A rejected flag looks exactly like a failed download otherwise.
#
# Required: MODEL_FILE, MODEL_URL. Optional: MODELS_DIR, TARGET_UID, TARGET_GID.
set -eu

MODELS_DIR="${MODELS_DIR:-/models}"
TARGET_UID="${TARGET_UID:-0}"
TARGET_GID="${TARGET_GID:-0}"

if [ -z "${MODEL_FILE:-}" ] || [ -z "${MODEL_URL:-}" ]; then
    echo "MIKU_MODEL_FILE and MIKU_MODEL_URL must both be set to fetch a model" >&2
    exit 1
fi

TARGET="$MODELS_DIR/$MODEL_FILE"
PART="$TARGET.part"

if [ -s "$TARGET" ]; then
    echo "model already present: $MODEL_FILE"
else
    echo "downloading $MODEL_FILE from $MODEL_URL"
    # -c resumes a partial transfer, -O names the file, -S shows the server response
    # so a failure explains itself. No -q: a silent failure is indistinguishable
    # from a blocked network until somebody reads these logs.
    if ! wget -c -S -O "$PART" "$MODEL_URL"; then
        echo "Error: the download failed." >&2
        echo "  url:    $MODEL_URL" >&2
        echo "  target: $TARGET" >&2
        echo "  check that this host can reach that url and has room for the file" >&2
        exit 1
    fi
    # A proxy or an error page can answer 200 with something that is not a model.
    # The format starts with a fixed four byte magic, so ask before installing it.
    if [ ! -s "$PART" ] || [ "$(head -c 4 "$PART")" != "GGUF" ]; then
        echo "Error: what was downloaded is not a GGUF model." >&2
        echo "  first bytes: $(head -c 4 "$PART" | od -c | head -1)" >&2
        rm -f "$PART"
        exit 1
    fi
    mv "$PART" "$TARGET"
fi

# A bind mount the daemon cannot chown must not throw away a good download.
chown "$TARGET_UID:$TARGET_GID" "$TARGET" 2>/dev/null ||
    echo "warning: could not chown the model to $TARGET_UID:$TARGET_GID, continuing"
chmod 0644 "$TARGET" 2>/dev/null ||
    echo "warning: could not chmod the model, continuing"

echo "model ready: $MODEL_FILE ($(du -h "$TARGET" | cut -f1))"
