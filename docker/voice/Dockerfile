# syntax=docker/dockerfile:1.7
# ── Voice runtime: recognition in C++, synthesis in Python ─────────────────────
# Two targets from one file because they are two halves of one feature and are
# versioned together, but they share nothing at runtime: the C++ server never loads
# a Python interpreter, and the synthesis service never links against it.
#
#   stt    whisper.cpp server, built from source. There is no published image for
#          it, and the alternative was a faster-whisper build that costs another
#          150 MB of memory for the same model in a different runtime.
#   voice  Python service holding the synthesis engines, one resident at a time.
#
# No cache mounts here: the daemon in use has no BuildKit, and a mount would make
# the build fail outright rather than merely rebuild. Base images are pinned by
# digest for the same reason the main image pins its own.

ARG WHISPER_VERSION=v1.9.4
ARG PYTHON_IMAGE=python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ARG DEBIAN_IMAGE=debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
# The voice conversion runtime: RVC-Project/Retrieval-based-Voice-Conversion-WebUI
# (MIT), pinned by commit and fetched as a tarball. It is a few hundred files and is
# deliberately not copied into this repository - a vendored fork in-tree is a thing
# nobody keeps honest, and this way the pin is the whole story.
ARG RVC_VERSION=81eed5e8f68b6bed1789f682fe78cdd324495afc
ARG ALPINE_IMAGE=alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc

# ── stt builder ───────────────────────────────────────────────────────────────
FROM ${DEBIAN_IMAGE} AS stt-builder
ARG WHISPER_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# Pinned to a release rather than a branch: an unpinned clone turns a working
# speech service into a rebuild that may not build.
RUN git clone --depth 1 --branch "$WHISPER_VERSION" https://github.com/ggml-org/whisper.cpp.git .
# No BLAS and no native tuning: both would add a shared library the runtime stage
# would have to carry, for a service whose whole point is to be small and portable.
# WHISPER_BUILD_EXAMPLES stays on on purpose: the server target lives under
# examples/, and switching it off leaves nothing to install. The default target is
# built rather than the single server, because the install step lays down every
# declared target - including shared objects this service links against - and
# refuses to run against a partial build.
RUN cmake -B build \
        -DWHISPER_BUILD_SERVER=ON \
        -DWHISPER_BUILD_TESTS=OFF \
        -DGGML_NATIVE=OFF \
        -DGGML_BLAS=OFF \
        -DGGML_CUDA=OFF \
    && cmake --build build -j "$(nproc)" \
    && cmake --install build --prefix /opt/whisper

FROM ${DEBIAN_IMAGE} AS stt
# ffmpeg is what the server shells out to for container formats; a browser sends
# webm or mp4, and without this only raw wav would reach the model.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# The whole install prefix, not just the binary: the server links against the
# whisper and ggml shared objects it was built beside, and copying the executable
# alone yields a bare "cannot open shared object file" on start.
COPY --from=stt-builder /opt/whisper /opt/whisper
ENV LD_LIBRARY_PATH=/opt/whisper/lib
USER 65534:65534
EXPOSE 8080

# ── voice builder ─────────────────────────────────────────────────────────────
FROM ${ALPINE_IMAGE} AS rvc-src
# Busybox wget and tar are enough for this, so the smallest image already pinned in
# the repository does the fetching and nothing else is installed to do it.
ARG RVC_VERSION
RUN set -eux; \
    mkdir -p /opt/rvc; \
    wget -q -O /tmp/rvc.tar.gz "https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/archive/${RVC_VERSION}.tar.gz"; \
    tar -xzf /tmp/rvc.tar.gz --strip-components=1 -C /opt/rvc; \
    # Only the inference half. Training brings in more dependencies than this service
    # has any use for, and a runtime that can only convert is the whole requirement.
    rm -rf /opt/rvc/train /opt/rvc/tools/pymss /opt/rvc/RVCRealtimeVST /opt/rvc/docs; \
    test -d /opt/rvc/infer/vc && test -d /opt/rvc/infer/module

FROM ${PYTHON_IMAGE} AS voice-builder
# CPU-only torch: the synthesis engines run on the same box as the database, and a
# CUDA build would drag in a driver stack for a GPU that may not be there.
ENV UV_TORCH_BACKEND=cpu UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# The uv image ships the two binaries and no shell, so it is a donor rather than a
# base; this is the same trick the main image uses.
COPY --from=ghcr.io/astral-sh/uv:0.11.18@sha256:78bc42400d77b0678ba95765305c826652ed5431f399257271dda681d0318f03 /uv /uvx /bin/
WORKDIR /build
COPY docker/voice/requirements.txt /build/requirements.txt
# Built at the path it will live at: a venv copied from elsewhere keeps the
# interpreter path baked into every script's shebang, and the process then fails to
# exec with a bare "no such file or directory".
RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv -r /build/requirements.txt

FROM ${PYTHON_IMAGE} AS voice
# The base image already has a system group called voice, so the group is made
# explicitly rather than letting useradd derive one and fail.
RUN groupadd --gid 1001 voiceapp && useradd --uid 1001 --gid 1001 --create-home voice
COPY --from=voice-builder /opt/venv /opt/venv
# The conversion runtime, imported as a library rather than run as a program: no
# browser tab, no upload, one model held in memory for the life of the process.
COPY --from=rvc-src /opt/rvc /opt/rvc
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONPATH="/opt/rvc"
WORKDIR /app
# Only the voice package: it must not be able to reach the database, the module
# registry or anything else the web process owns.
COPY app/voice /app/app/voice
RUN touch /app/app/__init__.py && chown -R 1001:1001 /app
USER 1001:1001
EXPOSE 8080
CMD ["uvicorn", "app.voice.service:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
