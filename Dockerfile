# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12-slim
ARG NETSANCTUM_MODULES=default
ARG NETSANCTUM_EXTERNAL_MODULES=""

FROM denoland/deno:bin-2.9.5@sha256:0d1262facd139e815217c001945eb822c7a78584cf660142c34a6b53effec1aa AS deno-bin

FROM ${PYTHON_IMAGE} AS dependencies

ARG NETSANCTUM_MODULES
ARG NETSANCTUM_EXTERNAL_MODULES

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /build

COPY pyproject.toml uv.lock module-build.json ./
COPY scripts/module_build.py scripts/module_build.py
COPY --from=deno-bin /deno /tmp/deno

RUN python scripts/module_build.py sync \
      --catalog module-build.json \
      --modules "${NETSANCTUM_MODULES}" \
      --external-modules "${NETSANCTUM_EXTERNAL_MODULES}" \
      --project /build \
      --environment /opt/venv \
      --marker /opt/netsanctum/installed-modules && \
    system_packages="$(python scripts/module_build.py system \
      --catalog module-build.json \
      --modules "${NETSANCTUM_MODULES}")" && \
    case " ${system_packages} " in \
      *" deno "*) mkdir -p /opt/netsanctum/bin && install -m 0755 /tmp/deno /opt/netsanctum/bin/deno ;; \
    esac && \
    rm /tmp/deno


FROM ${PYTHON_IMAGE} AS runtime

ARG NETSANCTUM_MODULES
ARG NETSANCTUM_EXTERNAL_MODULES
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/netsanctum/bin:/opt/venv/bin:${PATH}" \
    INSTALLED_MODULES_FILE=/opt/netsanctum/installed-modules \
    REQUIRE_INSTALLED_MODULES_MARKER=1

WORKDIR /app

COPY module-build.json /tmp/netsanctum-build/module-build.json
COPY scripts/module_build.py /tmp/netsanctum-build/module_build.py

RUN set -eu; \
    system_packages="$(python /tmp/netsanctum-build/module_build.py system \
      --catalog /tmp/netsanctum-build/module-build.json \
      --modules "${NETSANCTUM_MODULES}")"; \
    apt_packages=""; \
    case " ${system_packages} " in *" ffmpeg "*) apt_packages="${apt_packages} ffmpeg" ;; esac; \
    case " ${system_packages} " in *" nodejs "*) apt_packages="${apt_packages} nodejs" ;; esac; \
    if [ -n "${apt_packages}" ]; then \
      apt-get update; \
      apt-get install -y --no-install-recommends ${apt_packages}; \
    fi; \
    rm -rf /var/lib/apt/lists/* /tmp/netsanctum-build

COPY --from=dependencies /opt/venv /opt/venv
COPY --from=dependencies /opt/netsanctum /opt/netsanctum

COPY . .

RUN mkdir -p /app/storage && \
    python -c "from app.core.modules import module_registry; print(module_registry.diagnostics())" && \
    groupadd --gid "${APP_GID}" netsanctum && \
    useradd --uid "${APP_UID}" --gid "${APP_GID}" --home-dir /app --shell /usr/sbin/nologin netsanctum && \
    chown -R netsanctum:netsanctum /app/storage

USER netsanctum

EXPOSE 8000

CMD ["sh", "-c", "python -m app.core.migrations upgrade && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
