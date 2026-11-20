# syntax=docker/dockerfile:1
# Service web de Research-Agent. Ollama et SearXNG tournent dans d'autres conteneurs :
# celui-ci n'embarque ni GPU ni modèle, il orchestre seulement.

# Image de base figée par son empreinte : Dependabot propose une PR à chaque reconstruction.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS base

# uv (gestionnaire de paquets) : un étage FROM pour que Dependabot suive aussi sa version.
FROM ghcr.io/astral-sh/uv:0.13.9 AS uv

# --- Étape de construction : installe les dépendances exactes de uv.lock -------------
FROM base AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# 1) Les dépendances seules : cette couche reste en cache tant que uv.lock ne change pas.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra web --no-install-project
# 2) Le code de l'agent.
COPY research_agent ./research_agent
RUN uv sync --frozen --no-dev --extra web --no-editable

# --- Image finale : Python + l'environnement virtuel, sans uv ni fichiers de build ----
FROM base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    RA_DATA_DIR=/data \
    RA_CACHE_DIR=/data/cache
RUN useradd --uid 1000 --create-home agent \
 && mkdir -p /data && chown agent:agent /data
COPY --from=build /app/.venv /app/.venv

USER agent
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=4)"
ENTRYPOINT ["python", "-m", "research_agent.web", "--port", "8000"]
CMD ["--host", "127.0.0.1"]
