# VMware AI Ops Agent

AI-powered proactive maintenance agent for VMware vROps and vRLI using LLMs.

## Tech Stack
- **Language**: Python 3.11+
- **LLM**: OpenAI-compatible API (LightLLM/vLLM/OpenAI)
- **Vector DB**: FAISS
- **CLI**: Typer, Rich
- **Scheduling**: APScheduler
- **Metrics**: Prometheus client
- **Logging**: structlog
- **CI/CD**: GitLab CI

## Development Commands

```bash
# Setup virtual environment
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the agent CLI
vmware-ai-agent --help

# Run tests
pytest --cov=src

# Code quality
ruff check .
ruff format .
mypy src/

# Docker
docker-compose up -d
```

## Project Structure
- `src/` - Main application source code
- `tests/` - Test suite
- `config/` - Configuration files
- `deploy/` - Deployment manifests
- `scripts/` - Utility scripts
- `build/` - Build artifacts

## Key Features
- Proactive anomaly detection
- AI-powered root cause analysis
- Integration with vROps and vRLI APIs
- Vector embeddings for log analysis
- Scheduled maintenance recommendations

## Suggested Claude Code Plugins

### MCP Servers
- **filesystem** - For editing configuration files
- **docker** - Container management
- **openai** - For testing LLM integrations

### Skills
- **nsx-avi-reference** - For VMware infrastructure context

### Recommended Workflow
1. Use `ruff check` and `ruff format` before committing
2. Run `pytest` with coverage
3. Test LLM prompts in isolation before integration
4. Monitor with Prometheus metrics
5. Use structlog for consistent log formatting
