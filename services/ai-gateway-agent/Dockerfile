FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AIGW_CONFIG=/app/config/config.yaml

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config ./config

RUN useradd --create-home --uid 10001 aigw \
    && mkdir -p /app/out /app/data \
    && chown -R aigw:aigw /app
USER aigw

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["aigw"]
CMD ["serve"]
