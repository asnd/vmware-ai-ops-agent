# Security

Security controls span four areas: knowledge-base integrity, secret handling,
LLM prompt hygiene, and remediation guardrails.

## FAISS knowledge-base integrity (HMAC-SHA256)

Loading a FAISS index requires `allow_dangerous_deserialization=True` because the
metadata is a Python pickle — a tampered `index.pkl` is arbitrary-code-execution
risk. The `KnowledgeBase` (`analysis/knowledge_base.py`) defends against this:

- On **save**, it builds a manifest `{filename: sha256}` covering **both**
  `index.faiss` and `index.pkl`, then writes:
  - `index.manifest` — the JSON manifest, and
  - `index.hmac` — HMAC-SHA256 of the manifest bytes.
- On **load**, it recomputes the HMAC and re-hashes both files; any mismatch
  (`hmac.compare_digest`) is treated as tampering — the index is rejected and the
  agent starts with a fresh, empty store rather than deserialising untrusted data.

Fallbacks are handled explicitly: a legacy single-file HMAC (faiss only) is
accepted with a warning; a missing signature (first-ever load) is accepted with a
warning.

### The signing secret

The HMAC key derives from `knowledge_base.signing_secret`. If that is empty, the
code falls back to the **LLM API key** and logs a deprecation warning. **Set a
dedicated secret in production** so KB integrity is decoupled from your LLM
credential:

```
VMWARE_AI_KNOWLEDGE_BASE__SIGNING_SECRET=<random-32+-byte-secret>
```

(The code's warning references `VMWARE_AI__KNOWLEDGE_BASE__SIGNING_SECRET`; use
the env form your `pydantic-settings` prefix produces — verify with
`vmware-ai-agent validate`.)

## Secret handling

- Every credential field in `config.py` is a Pydantic `SecretStr`, so secrets are
  masked in reprs and structured logs.
- Secrets are supplied via env vars (`${VAR}` YAML placeholders or
  `VMWARE_AI_*__*`), never committed. See [configuration.md](configuration.md).
- Kubernetes injects them from a `Secret`; Compose from the shell environment.
- `_validate_required_secrets()` fails fast if a non-default host lacks its
  credential.

## LLM prompt scrubbing

Before any infrastructure text reaches the LLM, `analysis/llm_engine.py` runs it
through `utils/security.py:scrub_sensitive_data()`, which redacts:

- IPv4 / IPv6 addresses → `[REDACTED_IP]`
- email addresses → `[REDACTED_EMAIL]`
- UUIDs (common VMware identifiers) → `[REDACTED_UUID]`
- JWTs → `[REDACTED_JWT]`
- base64 `Authorization` headers and `key: value` secret patterns → `[REDACTED]`

This limits what a hosted/third-party model can see. The metrics, alerts, and
logs sections are all scrubbed in `analyze_infrastructure()`.

## MCP transport

`BaseMCPClient` sends a bearer token (`Authorization: Bearer <auth_token>`) when
configured, pins the MCP protocol version header, and only retries on
transport-level errors — it does **not** silently retry MCP application errors,
which surface as `RuntimeError`. Use `verify_ssl`/TLS on the MCP endpoints in
production and scope the tokens to read-only unless write ops are needed.

## Remediation guardrails

Remediation is defense-in-depth and **off by default**:

1. **Master switch** — `agent.auto_remediate.enabled=false` means no plan ever
   runs.
2. **Plans are never auto-executable** — `LLMAnalysisEngine` hardcodes
   `auto_executable=False` and `requires_approval=True` on every step, so
   `should_remediate()` routes to `END`.
3. **Allow / forbid lists** — `ActionExecutor` skips actions not in
   `allowed_actions` and any in `forbidden_actions` (default forbids
   `vm_power_off`, `host_maintenance_mode`).
4. **Approval gating** — only `NOTIFY` and `INVESTIGATE` are "safe"; everything
   else requires an approval callback, and a missing callback means the action is
   skipped, not run.
5. **Rate limiting** — at most `max_actions_per_hour` successful actions.
6. **Deduplication** — identical `(action_type, target)` within a 30-minute
   window is skipped, preventing thrash across cycles.
7. **Dry run** — `vcenter.dry_run=true` logs vCenter actions without executing
   them.

To actually act, you must deliberately enable the master switch, supply an
approval callback, and disable dry run — three independent decisions.

## Containers

Images run as a non-root user (`agent`, uid 1001 in `Containerfile` / 1000 in the
K8s `securityContext`), install only `ca-certificates` + `curl`, and mount config
read-only.
