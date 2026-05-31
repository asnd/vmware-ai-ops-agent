# Troubleshooting

Symptoms, likely causes, and where to look. The agent is designed to **degrade**
rather than crash, so most problems show up as warnings in the logs and empty/zero
results — not exceptions.

## Configuration & startup

**`<SECRET> is required when <host> is set to a non-default host`**
`_validate_required_secrets()` fired. You pointed a section at a real host but
didn't supply its credential. Export the env var (e.g. `VROPS_PASSWORD`) or set
`VMWARE_AI_<SECTION>__PASSWORD`. See [configuration.md](configuration.md).

**`Configuration file not found`**
No file at the `--config` path and none in the search order. Run
`vmware-ai-agent init -o config/settings.local.yaml`.

**Secrets look empty even though env vars are set**
`${VAR}` YAML placeholders expand a *bare* env var of that exact name; the
`VMWARE_AI_*__*` form is separate. Don't mix them up — see the two mechanisms in
[configuration.md](configuration.md#two-ways-to-inject-secrets).

## MCP connectivity

**`Failed to connect AriaOps MCP client` / `Failed to connect EntRAG MCP client`**
The connect handshake failed; the agent sets that client to `None` and continues.
Collection then falls back to empty AriaOps data (vRLI still runs) and enrichment
skips KB/capacity. Check the MCP `url`, that the server is reachable and healthy
(`GET /health` on the MCP server), and the `auth_token`.

**`MCP tool error: ...`**
An application-level error from the MCP server (surfaced as `RuntimeError`, not
retried). Inspect the MCP server's logs; verify the tool exists and arguments are
valid for that server version.

**Calls time out / cycle hits the 120s limit**
`_collect_infrastructure_state` bounds collection at 120s. A slow Aria Operations
or vRLI backend will trip `Infrastructure collection timed out after 120s`. Raise
the MCP client `timeout`, or reduce the resource kinds collected.

## Knowledge base / FAISS

**`FAISS index manifest HMAC verification failed — possible tampering`**
The index or manifest changed without a matching signature, or the signing secret
differs from when it was written. If you rotated `knowledge_base.signing_secret`
(or previously relied on the LLM-key fallback and then set a real secret), the old
index can't be verified — delete `data/faiss/` to start fresh, or restore the
original secret. See [security.md](security.md).

**`No HMAC signature found for FAISS index — skipping integrity check`**
First-ever load or an index written before signing existed. Harmless; the next
flush writes a manifest + HMAC.

**`No API key provided, knowledge base disabled`**
`llm.api_key` is empty, so embeddings (and thus the KB) are off. Set `LLM_API_KEY`.
Similar-incident enrichment will be empty until then.

## LLM analysis

**Analysis summary is the raw model text, urgency `medium`, no predictions**
The model didn't return parseable JSON, so `analyze_infrastructure` fell back to
treating the whole response as the summary. Check the model supports the JSON
contract; lower `temperature`; confirm `model`/`endpoint` are correct.

**`LLM request failed` then retries**
Transient endpoint errors are retried 3× with backoff. Persistent failures mean a
bad `endpoint`, wrong `api_key`, or the model server being down (in Compose,
`lightllm` needs a GPU and a model under `MODEL_PATH`).

## Remediation

**Plans never execute**
By design. `auto_remediate.enabled` defaults to `false`, the LLM marks every plan
`auto_executable=False`, and `vcenter.dry_run` defaults to `true`. To act you must
change all three and provide an approval callback. See
[security.md](security.md#remediation-guardrails).

**Actions skipped with `Approval required - no approval callback`**
A non-safe action needs human approval but no callback is wired. Expected unless
you've integrated one.

**Actions skipped with `Duplicate action within 30min window` / `Rate limit`**
The dedup window or `max_actions_per_hour` blocked it. Tune the config or wait out
the window.

## Notifications

**No Slack/Email despite a critical analysis**
`notify_analysis` only fires for CRITICAL/HIGH urgency and only via Slack; email
goes through `notify_issue`. Confirm the channel is `enabled`, the
`webhook_url`/SMTP settings are set, and `recipients` is non-empty for email.
ServiceNow delivery is not yet implemented (config-only).

## CLI output quirks

**`analyze` shows `Findings: 0` even when issues exist**
The `analyze` command reads a `findings` attribute that `AnalysisResult` doesn't
expose; rely on the **Urgency**, **Summary**, and **Predictions** rows instead, or
use `--format json` and read `predictions`.

## General debugging

- Add `--log-level DEBUG` (or set `logging.format: console`) for readable,
  verbose logs.
- Watch `:9090/metrics` — `analysis_cycles_total{status="error"}` climbing means
  the whole cycle is throwing; `partial_error` means a node degraded. See
  [observability.md](observability.md).
- Run a single pass with `vmware-ai-agent analyze` to reproduce without the
  scheduler.
