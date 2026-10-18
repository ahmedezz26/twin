# The twin's backend as a container: the FastAPI app served by uvicorn on port 8000.
# Lambda doesn't use this image (deploy.py builds a zip for it); this runs the same app anywhere Docker runs.
# Build from the repo root: docker build -t twin-backend backend

# uv, only to install the dependencies. Images are pinned by version and digest, like the GitHub Actions.
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

# ---- Stage 1: build. Installs the locked dependencies into a virtual environment.
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS build
COPY --from=uv /uv /bin/uv

# Compile to bytecode now (faster start), copy files out of uv's cache (it's a separate mount),
# skip the dev group, and use this image's Python instead of downloading one
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=0

WORKDIR /app/backend
# Only pyproject.toml and uv.lock are needed here, so editing the code doesn't redo this slow step.
# The cache mount keeps downloaded packages between builds without putting them in the image.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# ---- Stage 2: runtime. Python, the installed dependencies and the app; no uv, no cache.
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime

# Run as an unprivileged user, never root
RUN groupadd --system --gid 999 twin \
 && useradd --system --gid twin --uid 999 --no-create-home twin

# Same layout as the repo, so config.py finds memory/ and knowledge/ next to backend/ (in /app)
WORKDIR /app/backend
COPY --from=build /app/backend/.venv .venv
COPY data ./data
COPY *.py ./
# The only folders the app may write to; the code and the libraries stay read-only (owned by root)
RUN mkdir /app/memory /app/knowledge && chown twin:twin /app/memory /app/knowledge

ENV PATH="/app/backend/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER twin
EXPOSE 8000
# 0.0.0.0 = listen on the container's network interface, not only its own loopback, so a published port reaches it
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
