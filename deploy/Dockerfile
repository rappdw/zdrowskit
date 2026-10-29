# zdrowskit daemon image: health ingestion and the scheduled/Telegram flows.
#
# Build context is the repository root (see compose.yaml). State lives outside
# the image under ZDROWSKIT_HOME, which compose mounts as a volume.

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 \
    ZDROWSKIT_HOME=/data

# Kaleido renders chart PNGs through a Chromium-based browser and finds
# Debian's `chromium` on PATH. Google Chrome has no Linux arm64 build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 zdrowskit \
    && mkdir -p /data /opt/venv \
    && chown zdrowskit:zdrowskit /data /opt/venv

WORKDIR /app

# Dependencies first, so source edits don't invalidate the dependency layer.
COPY --chown=zdrowskit:zdrowskit pyproject.toml uv.lock .python-version ./
USER zdrowskit
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=zdrowskit:zdrowskit . .
RUN uv sync --frozen --no-dev

CMD ["uv", "run", "--no-sync", "python", "src/daemon.py", "--foreground"]
