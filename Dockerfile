# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12-slim
ARG NETSANCTUM_MODULES=default
ARG NETSANCTUM_EXTERNAL_MODULES=""

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

RUN python scripts/module_build.py sync \
      --catalog module-build.json \
      --modules "${NETSANCTUM_MODULES}" \
      --external-modules "${NETSANCTUM_EXTERNAL_MODULES}" \
      --project /build \
      --environment /opt/venv \
      --marker /opt/netsanctum/installed-modules


FROM ${PYTHON_IMAGE} AS runtime

ARG NETSANCTUM_MODULES
ARG NETSANCTUM_EXTERNAL_MODULES
ARG DENO_VERSION=2.9.5
ARG DENO_SHA256=8b010a3b1a4a0188a67cdb8a7a27348b2a501af78aec7fc74f2ace167368d530

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
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
    case " ${system_packages} " in *" deno "*) apt_packages="${apt_packages} ca-certificates curl unzip" ;; esac; \
    if [ -n "${apt_packages}" ]; then \
      apt-get update; \
      apt-get install -y --no-install-recommends ${apt_packages}; \
    fi; \
    case " ${system_packages} " in \
      *" deno "*) \
        curl -fsSL "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" -o /tmp/deno.zip; \
        printf '%s  %s\n' "${DENO_SHA256}" /tmp/deno.zip | sha256sum -c -; \
        unzip /tmp/deno.zip -d /usr/local/bin; \
        rm /tmp/deno.zip; \
        apt-get purge -y curl unzip; \
        ;; \
    esac; \
    rm -rf /var/lib/apt/lists/* /tmp/netsanctum-build

COPY --from=dependencies /opt/venv /opt/venv
COPY --from=dependencies /opt/netsanctum /opt/netsanctum

COPY . .

RUN mkdir -p /app/storage && \
    python -c "from app.core.modules import module_registry; print(module_registry.diagnostics())"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
