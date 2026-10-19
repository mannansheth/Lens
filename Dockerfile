FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EMBEDDING_CACHE_DIR=/var/lib/lens/models

WORKDIR /opt/lens
COPY pyproject.toml README.md ./
COPY src ./src
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir '.[gemini]' \
    && useradd --system --create-home --home-dir /home/lens lens \
    && mkdir -p /var/lib/lens/models \
    && chown -R lens:lens /var/lib/lens /opt/lens

USER lens
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=3s --start-period=180s --retries=4 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2)"
ENTRYPOINT ["lens"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765", "--project", "/workspace"]
