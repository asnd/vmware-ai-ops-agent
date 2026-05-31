# CLI Reference

The package installs a `vmware-ai-agent` console script
(`pyproject.toml` → `vmware_ai_ops_agent.cli:app`). It is a [Typer](https://typer.tiangolo.com/)
app with five commands. Source: `src/vmware_ai_ops_agent/cli.py`.

```bash
vmware-ai-agent --help
```

## `run` — continuous monitoring

Starts the agent: initialises the knowledge base, connects MCP clients, starts
the Prometheus server, runs one cycle immediately, then repeats on the schedule.

```bash
vmware-ai-agent run --config config/settings.local.yaml
```

| Option | Alias | Default | Description |
|--------|-------|---------|-------------|
| `--config` | `-c` | (search order) | Path to the config file. |
| `--interval` | `-i` | from config | Override `agent.cycle_interval` (seconds). |
| `--log-level` | `-l` | `INFO` | Log level; `DEBUG` switches structlog to console rendering. |
| `--metrics-port` | | from config | Override the Prometheus port. |

Runs until interrupted (Ctrl-C), then shuts down cleanly: stops the scheduler,
disconnects MCP clients, and flushes pending KB documents.

## `analyze` — one-shot analysis

Runs a single graph invocation and prints the result, then exits. Good for cron,
CI, or ad-hoc checks.

```bash
# Human-readable table
vmware-ai-agent analyze -c config/settings.local.yaml

# JSON, to a file
vmware-ai-agent analyze -c config/settings.local.yaml --format json --output result.json
```

| Option | Alias | Default | Description |
|--------|-------|---------|-------------|
| `--config` | `-c` | (search order) | Path to the config file. |
| `--output` | `-o` | stdout | Write results to a file instead of the console. |
| `--format` | `-f` | `text` | `text` (Rich table) or `json`. |

Output includes urgency, summary, and predicted failures with probabilities.

## `status` — show state

Prints a snapshot from `VMwareAIOpsAgent.get_status()`: running flag, total
cycles, last cycle time, issues detected, actions executed, last-analysis
urgency, knowledge-base statistics, and MCP client status.

```bash
vmware-ai-agent status -c config/settings.local.yaml
```

> Note: `status` instantiates a fresh agent to read configuration-derived state;
> live counters (cycles, issues) reflect a running process only when wired to one.

## `capacity` — capacity / time-to-exhaustion report

Connects to the Aria Operations MCP server and reports, per resource, the
remaining capacity and estimated days to exhaustion (soonest first; rows under
30 days are highlighted). Requires `ariaops_mcp.enabled: true`.

```bash
vmware-ai-agent capacity -c config/settings.local.yaml --kind Datastore
vmware-ai-agent capacity -c config/settings.local.yaml -k HostSystem -n 50 --format json
```

| Option | Alias | Default | Description |
|--------|-------|---------|-------------|
| `--config` | `-c` | (search order) | Path to the config file. |
| `--kind` | `-k` | `HostSystem` | Resource kind to report on (e.g. `Datastore`, `ClusterComputeResource`). |
| `--limit` | `-n` | `20` | Max resources to report. |
| `--format` | `-f` | `text` | `text` (Rich table) or `json`. |

## `validate` — check config & policy

Loads the config, runs secret validation, and prints the configured vROps / vRLI
/ vCenter / LLM endpoints plus the auto-remediation policy (enabled, approval,
rate limit, allowed/forbidden actions). Exits non-zero on any failure.

```bash
vmware-ai-agent validate -c config/settings.local.yaml
```

## `init` — generate a sample config

Writes a starter `settings.yaml` with `${VAR}` placeholders for secrets.

```bash
vmware-ai-agent init --output config/settings.local.yaml
# overwrite an existing file
vmware-ai-agent init -o config/settings.local.yaml --force
```

| Option | Alias | Default | Description |
|--------|-------|---------|-------------|
| `--output` | `-o` | `./config/settings.yaml` | Where to write the sample. |
| `--force` | `-f` | `false` | Overwrite if the file exists. |

## Typical first run

```bash
pip install -e ".[dev]"
vmware-ai-agent init -o config/settings.local.yaml
# edit the file, then export the referenced env vars (VROPS_PASSWORD, LLM_API_KEY, ...)
vmware-ai-agent validate -c config/settings.local.yaml
vmware-ai-agent analyze  -c config/settings.local.yaml
vmware-ai-agent run      -c config/settings.local.yaml
```
