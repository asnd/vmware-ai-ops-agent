# VMware AI Ops Agent

AI-powered proactive maintenance agent for VMware vRealize Operations (vROps) and vRealize Log Insight (vRLI).

## Features

- **Predictive Failure Detection**: AI analysis of metrics and logs
- **Root Cause Analysis**: Multi-source correlation
- **Automated Remediation**: Safe execution with guardrails
- **Pattern Library**: 15+ known issue patterns
- **Multi-channel Notifications**: Slack, Email, ServiceNow
- **Knowledge Base**: ChromaDB for incident history

## Quick Start

```bash
# Install
pip install -e .

# Configure
cp config/settings.yaml config/settings.local.yaml
# Edit with your credentials

# Run
vmware-ai-agent run --config config/settings.local.yaml
```

## Architecture

- `collectors/`: vROps & vRLI API clients
- `analysis/`: LLM engine & knowledge base
- `correlation/`: Pattern matching engine
- `actions/`: Remediation executor
- `cli.py`: Command interface

## Deployment

- Docker: `docker build -t vmware-ai-ops-agent .`
- Kubernetes: `kubectl apply -f deploy/kubernetes/`
- Docker Compose: `docker-compose up -d`

## License

MIT
