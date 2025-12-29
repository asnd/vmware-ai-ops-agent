# GEMINI Project Profile: VMware AI Ops Agent

## Project Overview
- **Name**: VMware AI Ops Agent
- **Primary Language**: Python 3.10+
- **Purpose**: AI-powered proactive maintenance agent for VMware vRealize Operations (vROps) and vRealize Log Insight (vRLI).
- **Key Features**: Predictive failure detection, root cause analysis (LLM-powered), automated remediation with guardrails, and multi-channel notifications.

## Project Structure
```
vmware-ai-ops-agent/
├── src/vmware_ai_ops_agent/ # Main source code
│   ├── collectors/          # Data collection (vROps, vRLI)
│   ├── analysis/            # AI/LLM analysis engine
│   ├── correlation/         # Issue correlation logic
│   ├── actions/             # Remediation executors
│   └── agent.py             # Main orchestrator
├── config/                  # Configuration templates
├── deploy/                  # K8s and Prometheus deployment files
├── tests/                   # Unit and integration tests
├── docker-compose.yaml      # Full stack orchestration
└── pyproject.toml           # Python dependencies and config
```

## Build & Deployment
- **Build System**: `pyproject.toml` (Setuptools/Wheel).
- **Containerization**: Docker & Docker Compose.
- **CI/CD**: Not explicitly defined in file list, but standard Python tooling (`pytest`, `ruff`) is configured.

## Suggested Development Tools
- **VSCode Extensions**:
  - `ms-python.python`: Python language support.
  - `charliermarsh.ruff`: Fast Python linter and formatter.
  - `tamasfe.even-better-toml`: TOML support for `pyproject.toml`.
- **CLI Tools**:
  - `ruff`: Linting and formatting.
  - `mypy`: Static type checking.
  - `pytest`: Testing framework.
- **MCP Servers**:
  - `filesystem`: For file access.
