# VMware AI Ops Agent — Documentation

AI-powered proactive maintenance agent for VMware Aria Operations (vROps) and
vRealize Log Insight (vRLI). It continuously collects infrastructure state,
correlates it against a library of known failure patterns, uses an LLM to
produce root-cause analysis and remediation plans, and can act on those plans
through guarded vCenter / Aria Operations operations.

This folder is the full documentation set. Start with the architecture overview,
then jump to whichever reference you need.

## Contents

| Page | What it covers |
|------|----------------|
| [architecture.md](architecture.md) | System overview, the LangGraph workflow, MCP composition vs. direct collectors, data flow. |
| [configuration.md](configuration.md) | Every `settings.yaml` section, the two env-var mechanisms, load order, secret validation. |
| [cli.md](cli.md) | The `vmware-ai-agent` CLI: `run`, `analyze`, `status`, `validate`, `init`. |
| [modules.md](modules.md) | Module/API reference for every package, key classes, and entry points. |
| [patterns.md](patterns.md) | The built-in correlation patterns, the `KnownPattern` schema, confidence scoring. |
| [deployment.md](deployment.md) | Docker, Podman/Containerfile, Compose stack, Kubernetes, GitLab CI. |
| [observability.md](observability.md) | Prometheus metrics catalog, structlog setup, health checks, Grafana. |
| [security.md](security.md) | FAISS HMAC integrity, secret handling, prompt scrubbing, remediation guardrails. |
| [extending.md](extending.md) | How to add patterns, collectors, remediation actions, MCP clients, notification channels. |
| [troubleshooting.md](troubleshooting.md) | Common failures and how to diagnose them from logs and metrics. |

## Roadmap

Proposed new features live in [../ROADMAP.md](../ROADMAP.md).

## Quick orientation

- **Language / runtime:** Python ≥ 3.11, fully async (`asyncio`).
- **Entry point:** the `vmware-ai-agent` console script → `vmware_ai_ops_agent.cli:app`.
- **Orchestration:** a LangGraph state machine (`graph.py`) driven by `VMwareAIOpsAgent` (`agent.py`).
- **Data in:** Aria Operations via the `ariaops_mcp` MCP server; vRLI logs via a direct collector.
- **Knowledge in:** Broadcom/VMware KB via the `entrag` MCP server; past incidents via a local FAISS store.
- **Intelligence:** an OpenAI-compatible LLM endpoint (LightLLM / vLLM / OpenAI).
- **Actions out:** vCenter (vMotion, storage vMotion, DRS), Aria Operations write ops (maintenance mode, alert modify), and Slack / Email / ServiceNow notifications.

See [architecture.md](architecture.md) for how these fit together.
