# VMware AI Ops Agent

AI-powered proactive maintenance agent for VMware vRealize Operations (vROps) and vRealize Log Insight (vRLI).

## Overview

VMware AI Ops Agent continuously monitors your VMware infrastructure, uses AI to detect anomalies and predict failures before they happen, and can automatically remediate issues with built-in safety guardrails.

## Documentation

Full documentation lives in [`docs/`](docs/README.md):
[architecture](docs/architecture.md) ·
[configuration](docs/configuration.md) ·
[CLI](docs/cli.md) ·
[modules/API](docs/modules.md) ·
[patterns](docs/patterns.md) ·
[deployment](docs/deployment.md) ·
[observability](docs/observability.md) ·
[security](docs/security.md) ·
[extending](docs/extending.md) ·
[troubleshooting](docs/troubleshooting.md).
Proposed features are tracked in [ROADMAP.md](ROADMAP.md).

## Features

- **Predictive Failure Detection**: LLM-powered analysis of metrics and logs to predict failures before they occur
- **Root Cause Analysis**: Multi-source correlation engine that connects alerts, metrics, and logs
- **Automated Remediation**: Safe execution framework with approval workflows, rate limiting, and forbidden action lists
- **Pattern Library**: 10+ known infrastructure issue patterns (APD, PDL, memory pressure, vMotion failures, etc.)
- **Multi-channel Notifications**: Slack, Email, ServiceNow integration
- **Knowledge Base**: FAISS vector store for incident history and similar case matching
- **Prometheus Metrics**: Full observability with custom metrics

## Architecture

```
                    ┌─────────────────┐
                    │   VMware AI     │
                    │   Ops Agent     │
                    └────────┬────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│    vROps      │   │    vRLI       │   │   vCenter     │
│  Collector    │   │  Collector    │   │   Client      │
└───────┬───────┘   └───────┬───────┘   └───────┬───────┘
        │                   │                   │
        └───────────┬───────┘                   │
                    ▼                           │
           ┌───────────────┐                    │
           │  Correlation  │                    │
           │    Engine     │                    │
           └───────┬───────┘                    │
                   ▼                            │
           ┌───────────────┐                    │
           │  LLM Analysis │                    │
           │    Engine     │                    │
           └───────┬───────┘                    │
                   ▼                            │
           ┌───────────────┐                    │
           │   Action      │◄───────────────────┘
           │   Executor    │
           └───────┬───────┘
                   │
        ┌──────────┼──────────┐
        ▼          ▼          ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐
    │  Slack  │ │  Email  │ │ServiceNow│
    └─────────┘ └─────────┘ └─────────┘
```

### Multi-vCenter + NSX topology options (single vROps instance)

When one vROps instance manages multiple vCenters, and each vCenter has its own NSX Manager (1:1), two viable architectures are:

1. **Hub-and-spoke inventory model (recommended)**
   - Keep one `vrops` collector as the source of truth for inventory/alerts.
   - Add a `vcenter_targets[]` list where each item contains:
     - `vcenter_id`
     - `vcenter` connection settings
     - `nsx_manager` connection settings
   - Resolve every remediation target to `vcenter_id` first, then use that mapping to call the paired NSX Manager.
   - Pros: centralized correlation, deterministic vCenter↔NSX routing, easy safety policy per pair.

2. **Per-domain execution pipeline**
   - Build independent execution contexts (`domain`) per pair: `(vCenter, NSX Manager)`.
   - Broadcast vROps findings into all domains, then filter by ownership tags/object lineage before actioning.
   - Pros: stronger blast-radius isolation and clearer multi-tenant boundaries.

**Suggested mapping contract**

```yaml
vrops:
  host: vrops.example.com

vcenter_targets:
  - vcenter_id: vc-prod-01
    vcenter:
      host: vc-prod-01.example.com
      username: ${VC01_USERNAME}
      password: ${VC01_PASSWORD}
    nsx_manager:
      host: nsx-prod-01.example.com
      username: ${NSX01_USERNAME}
      password: ${NSX01_PASSWORD}
  - vcenter_id: vc-prod-02
    vcenter:
      host: vc-prod-02.example.com
      username: ${VC02_USERNAME}
      password: ${VC02_PASSWORD}
    nsx_manager:
      host: nsx-prod-02.example.com
      username: ${NSX02_USERNAME}
      password: ${NSX02_PASSWORD}
```

## Quick Start

### Installation

```bash
# Clone repository
git clone https://gitlab.com/nikosa/vmware-ai-ops-agent.git
cd vmware-ai-ops-agent

# Install with pip
pip install -e .

# Or with dev dependencies
pip install -e ".[dev]"
```

### Configuration

```bash
# Generate sample configuration
vmware-ai-agent init --output config/settings.local.yaml

# Edit with your credentials
# Set environment variables for secrets:
export VROPS_USERNAME=admin
export VROPS_PASSWORD=secret
export VRLI_USERNAME=admin
export VRLI_PASSWORD=secret
export VCENTER_USERNAME=administrator@vsphere.local
export VCENTER_PASSWORD=secret
export LLM_API_KEY=your-api-key
```

### Run

```bash
# Validate configuration
vmware-ai-agent validate --config config/settings.local.yaml

# Run continuous monitoring
vmware-ai-agent run --config config/settings.local.yaml

# Run single analysis
vmware-ai-agent analyze --config config/settings.local.yaml

# Check status
vmware-ai-agent status --config config/settings.local.yaml
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `run` | Run the agent continuously with scheduled analysis cycles |
| `analyze` | Run a single analysis cycle and exit |
| `status` | Show current agent status and statistics |
| `validate` | Validate configuration and connectivity |
| `init` | Generate a sample configuration file |

## Configuration Reference

```yaml
# vRealize Operations
vrops:
  host: vrops.example.com
  port: 443
  username: ${VROPS_USERNAME}
  password: ${VROPS_PASSWORD}
  verify_ssl: true

# vRealize Log Insight
vrli:
  host: vrli.example.com
  port: 443
  username: ${VRLI_USERNAME}
  password: ${VRLI_PASSWORD}

# LLM Endpoint (LightLLM/vLLM/OpenAI-compatible)
llm:
  endpoint: http://localhost:8000/v1
  api_key: ${LLM_API_KEY}
  model: mistral-7b-instruct
  max_tokens: 4096
  temperature: 0.1

# Agent Behavior
agent:
  cycle_interval: 300  # seconds between analysis cycles
  thresholds:
    cpu_critical: 90
    cpu_warning: 80
    memory_critical: 95
    disk_latency_critical: 50
  auto_remediate:
    enabled: false      # Enable automated remediation
    require_approval: true
    max_actions_per_hour: 10
    allowed_actions:
      - vmotion
      - drs_rebalance
      - snapshot_cleanup
    forbidden_actions:
      - vm_power_off
      - host_maintenance_mode
```

## Known Issue Patterns

The correlation engine includes patterns for detecting:

| Pattern | Description |
|---------|-------------|
| APD (All Paths Down) | Storage connectivity issues |
| PDL (Permanent Device Loss) | Storage device failures |
| Memory Pressure | Host memory exhaustion |
| Storage Latency | High disk I/O latency |
| vMotion Failure | VM migration issues |
| HA Failover | High Availability events |
| Network Partition | vSAN network isolation |
| Snapshot Growth | Uncontrolled snapshot accumulation |
| License Expiry | vSphere license issues |
| Certificate Expiry | SSL certificate problems |

## Deployment

### Docker

```bash
docker build -t vmware-ai-ops-agent .
docker run -d \
  -v ./config:/app/config:ro \
  -e VROPS_USERNAME=admin \
  -e VROPS_PASSWORD=secret \
  vmware-ai-ops-agent
```

### Docker Compose

```bash
# Start full stack (agent + ariaops-mcp + entrag-mcp + LightLLM + Prometheus + Grafana)
docker-compose up -d

# View logs
docker-compose logs -f vmware-ai-agent
```

### Kubernetes

```bash
# Create namespace and secrets
kubectl apply -f deploy/kubernetes/deployment.yaml

# Update secrets with your credentials
kubectl -n vmware-ai-ops edit secret vmware-credentials

# Check deployment
kubectl -n vmware-ai-ops get pods
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
pytest --cov=vmware_ai_ops_agent

# Lint and format
ruff check src/
ruff format --check src/

# Type check
mypy src/
```

## Project Structure

```
vmware-ai-ops-agent/
├── src/vmware_ai_ops_agent/
│   ├── collectors/          # Data collection from vROps/vRLI
│   │   ├── models.py        # Data models
│   │   ├── vrops.py         # vROps API client
│   │   └── vrli.py          # vRLI API client
│   ├── analysis/            # AI analysis
│   │   ├── models.py        # Analysis result models
│   │   ├── llm_engine.py    # LLM-powered analysis
│   │   └── knowledge_base.py # FAISS integration
│   ├── correlation/         # Issue correlation
│   │   ├── patterns.py      # Known issue patterns
│   │   └── engine.py        # Correlation engine
│   ├── actions/             # Remediation
│   │   ├── executor.py      # Action execution
│   │   ├── vcenter.py       # vCenter API client
│   │   └── notifications.py # Notification services
│   ├── config.py            # Configuration management
│   ├── agent.py             # Main orchestrator
│   └── cli.py               # CLI interface
├── config/
│   └── settings.yaml        # Sample configuration
├── deploy/
│   ├── kubernetes/          # K8s manifests
│   └── prometheus/          # Prometheus config
├── tests/                   # Test suite
├── docker-compose.yaml      # Full stack deployment
├── Dockerfile               # Container build
└── pyproject.toml           # Python package config
```

## License

MIT

## Author

Security Research Team
