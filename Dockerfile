# Stage 1: Build & Compile dependencies dynamically
FROM python:3.12-slim AS requirements-builder

WORKDIR /tmp-build

# Install uv using the official binary image copy
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uv_bin/uv
ENV PATH="/uv_bin:${PATH}"

# Copy requirements.in files for core and all modules
COPY requirements.in .
COPY app/modules/auth/requirements.in ./app/modules/auth/
COPY app/modules/music/requirements.in ./app/modules/music/
COPY app/modules/settings/requirements.in ./app/modules/settings/
COPY app/modules/video_archiver/requirements.in ./app/modules/video_archiver/

# Compile unified requirements.txt dynamically
RUN uv pip compile requirements.in app/modules/*/requirements.in --generate-hashes --python 3.12 -o requirements.txt

# Stage 2: Final runtime container
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev ffmpeg nodejs && \
    rm -rf /var/lib/apt/lists/*

# Copy compiled requirements.txt from Stage 1
COPY --from=requirements-builder /tmp-build/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/storage

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
