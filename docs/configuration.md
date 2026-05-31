# Configuration

Configuration is defined by Pydantic models in `src/vmware_ai_ops_agent/config.py`
and loaded from YAML. A documented sample lives at `config/settings.yaml`;
generate your own with `vmware-ai-agent init` (see [cli.md](cli.md)).

## Load order

`load_settings(config_path)` resolves the config file in this order:

1. An explicit `--config` path (via `Settings.from_yaml`).
2. `./config/settings.local.yaml`
3. `./config/settings.yaml`
4. `/etc/vmware-ai-agent/settings.yaml`
5. If none exist, an all-defaults `Settings()`.

`settings.local.yaml` is the intended place for your real values and is checked
before the committed sample.

## Two ways to inject secrets

There are **two independent** mechanisms — know which one you're using:

1. **YAML `${VAR}` placeholders.** During `from_yaml`, any string value of the
   exact form `${NAME}` is replaced by the environment variable `NAME` (or an
   empty string if unset). These are *bare* names you choose in the YAML, e.g.
   `password: ${VROPS_PASSWORD}` reads `$VROPS_PASSWORD`. This is a custom
   expansion in `Settings._expand_env_vars`.

2. **Pydantic-settings env vars.** `Settings` uses `env_prefix="VMWARE_AI_"` and
   `env_nested_delimiter="__"`, so you can set any field directly from the
   environment without touching YAML. The pattern is
   `VMWARE_AI_<SECTION>__<FIELD>` (uppercase). Examples (used by the Compose and
   Kubernetes manifests):

   ```
   VMWARE_AI_VROPS__PASSWORD=...
   VMWARE_AI_VRLI__HOST=vrli.corp.example.com
   VMWARE_AI_LLM__API_KEY=...
   VMWARE_AI_ARIAOPS_MCP__URL=http://ariaops-mcp:8080/mcp
   VMWARE_AI_NOTIFICATIONS__SLACK__WEBHOOK_URL=...
   ```

In containers, prefer mechanism (2): mount a non-secret `settings.yaml` and pass
secrets as environment variables.

## Required-secret validation

After loading, `_validate_required_secrets()` enforces that a password / API key
is present **only when the corresponding host is non-default**. The checks are:

| Section | Triggers when host ≠ | Requires |
|---------|----------------------|----------|
| `vrops` | `vrops.example.com` | `VROPS_PASSWORD` |
| `vrli` | `vrli.example.com` | `VRLI_PASSWORD` |
| `vcenter` | `vcenter.example.com` | `VCENTER_PASSWORD` |
| `llm` | `http://localhost:8000/v1` | `LLM_API_KEY` |

So a fresh sample config validates fine; the moment you point at a real vCenter
without supplying its password, `validate` (and startup) fail with a clear error.

## Sections reference

All secret fields are Pydantic `SecretStr` (masked in logs/reprs).

### `vrops` — Aria Operations (REST fallback collector)
`host` `port=443` `username` `password` `verify_ssl=true` `timeout=30`.
Note: the live collection path uses `ariaops_mcp`; these settings feed the direct
`VROpsCollector` only.

### `vrli` — Log Insight
`host` `port=443` `username` `password` `verify_ssl=true` `timeout=30` and a
`query` map (`default_time_range: LAST_1_HOUR`, `max_results: 10000`). Valid time
ranges: `LAST_5_MINUTES`, `LAST_15_MINUTES`, `LAST_30_MINUTES`, `LAST_1_HOUR`,
`LAST_6_HOURS`, `LAST_12_HOURS`, `LAST_24_HOURS`.

### `vcenter` — remediation target
`host` `port=443` `username` `password` `verify_ssl=true` `dry_run=true`.
With `dry_run: true` (default), vMotion / storage vMotion / DRS actions are logged
but **not** executed.

### `llm` — OpenAI-compatible endpoint
`endpoint=http://localhost:8000/v1` `api_key` `model=mistral-7b-instruct`
`max_tokens=4096` `temperature=0.1` `max_retries=3`. Works with LightLLM, vLLM,
or OpenAI itself. The `api_key` doubles as the embedding key for the FAISS KB.

### `vector_db` — FAISS store
`type=faiss` `persist_directory=./data/faiss` `collection_name=vmware_incidents`.

### `agent` — cycle behaviour
- `cycle_interval=300` (seconds between cycles).
- `thresholds`: `cpu_critical=90` `cpu_warning=80` `memory_critical=95`
  `memory_warning=85` `disk_latency_critical=50` (ms).
- `auto_remediate`:
  - `enabled=false` — master switch.
  - `require_approval=true` — non-safe actions need an approval callback.
  - `max_actions_per_hour=10` — rate limit.
  - `allowed_actions=[vmotion, drs_rebalance, snapshot_cleanup]`.
  - `forbidden_actions=[vm_power_off, host_maintenance_mode]` — never run.

### `notifications`
- `slack`: `enabled=false` `webhook_url` `channel="#vmware-ops"` `mention_on_critical="@oncall"`.
- `email`: `enabled=false` `smtp_host` `smtp_port=587` `smtp_user` `smtp_password`
  `from_address` `recipients=[]`.
- `servicenow`: `enabled=false` `instance` `username` `password`.

> Implementation note: today `notify_analysis` delivers to Slack; `notify_issue`
> covers Slack + Email. ServiceNow config is modelled but delivery is not yet
> wired. See [troubleshooting.md](troubleshooting.md).

### `knowledge_base`
`runbooks_dir=./config/runbooks` `kb_cache_dir=./data/kb_cache`
`history_retention=90` (days) `signing_secret` — the dedicated HMAC key for FAISS
index integrity. **Set this in production.** See [security.md](security.md).

### `correlation`
`custom_patterns_file=""` — optional path to a YAML file of site-specific
correlation patterns merged with the built-ins at startup. Empty disables it.
The file is a `patterns:` list (or a top-level list); each entry mirrors the
`KnownPattern` fields, with `metric_conditions` values written as
`[operator, threshold]`. Invalid entries are skipped with a warning rather than
aborting startup. See [patterns.md](patterns.md) and
[extending.md](extending.md#add-a-correlation-pattern).

```yaml
correlation:
  custom_patterns_file: ./config/custom_patterns.yaml
```

### `logging`
`level=INFO` (`DEBUG|INFO|WARNING|ERROR`) `format=json` (`json|console`)
`file=./logs/agent.log`.

### `metrics`
`enabled=true` `port=9090` `path=/metrics`.

### `ariaops_mcp` — Aria Operations MCP server
`url=http://localhost:8080/mcp` `auth_token` `timeout=120.0` `enabled=true`.

### `entrag_mcp` — EntRAG KB MCP server
`url=http://localhost:8081/mcp` `auth_token` `timeout=60.0` `enabled=true`.

## Validating

```bash
vmware-ai-agent validate --config config/settings.local.yaml
```

This loads the file, applies secret validation, and prints the configured
endpoints and auto-remediation policy. See [cli.md](cli.md#validate).
