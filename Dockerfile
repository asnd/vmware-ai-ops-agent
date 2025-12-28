FROM python:3.11-slim

LABEL maintainer="Security Research Team"
LABEL description="AI-powered proactive maintenance agent for VMware vROps and vRLI"

WORKDIR /app

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy application
COPY pyproject.toml ./
COPY src/ ./src/
COPY config/ ./config/

# Install Python packages
RUN pip install --no-cache-dir -e .

# Create non-root user
RUN useradd --create-home --shell /bin/bash agent && \
    mkdir -p /app/data /app/logs && \
    chown -R agent:agent /app

USER agent

ENV PYTHONUNBUFFERED=1

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:9090/metrics || exit 1

ENTRYPOINT ["vmware-ai-agent"]
CMD ["run", "--config", "/app/config/settings.yaml"]
