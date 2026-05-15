# Multi-stage build for vmware-ai-ops-agent
# Use podman build --format docker for HEALTHCHECK support

FROM python:3.11-slim AS builder

WORKDIR /build

COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-cache-dir --prefix=/install .


FROM python:3.11-slim

LABEL maintainer="Security Research Team"
LABEL description="AI-powered proactive maintenance agent for VMware Aria Operations"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY src/ ./src/
COPY config/ ./config/

RUN useradd --create-home --uid 1001 --shell /bin/bash agent && \
    mkdir -p /app/data /app/logs && \
    chown -R agent:agent /app

USER agent

ENV PYTHONUNBUFFERED=1

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:9090/metrics || exit 1

ENTRYPOINT ["vmware-ai-agent"]
CMD ["run", "--config", "/app/config/settings.yaml"]
