# Service web de Research-Agent. Le modèle (Ollama) et SearXNG tournent à côté :
# le conteneur n'embarque ni GPU ni modèle, il orchestre seulement.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RA_DATA_DIR=/data \
    RA_CACHE_DIR=/data/cache

WORKDIR /app
COPY pyproject.toml README.md ./
COPY research_agent ./research_agent
RUN pip install ".[web]" \
 && useradd --uid 1000 --create-home agent \
 && mkdir -p /data && chown agent:agent /data

USER agent
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=4)"
ENTRYPOINT ["python", "-m", "research_agent.web", "--port", "8000"]
CMD ["--host", "127.0.0.1"]
