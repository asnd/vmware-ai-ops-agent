# Roadmap — Proposed Features

Forward-looking proposals for the VMware AI Ops Agent. These are **suggestions**,
not committed work, and intentionally build on what already exists (see
[docs/](docs/README.md)). Each entry lists the motivation, a design sketch, the
files it would touch, dependencies, and a rough effort estimate
(S ≈ <1 day, M ≈ 2–4 days, L ≈ 1–2 weeks).

Effort assumes one developer familiar with the codebase.

---

## 1. Event-driven trigger mode  ·  effort: M

**Problem.** Today the only way to run a cycle is the fixed `cycle_interval`
(default 300s) or the one-shot `analyze` command. A real outage shouldn't wait up
to five minutes for the next tick.

**Design.** Add a lightweight HTTP entrypoint (FastAPI) exposing
`POST /trigger` that calls the existing `VMwareAIOpsAgent.analyze_now()`. Accept
an optional payload (e.g. an Aria Operations webhook or alertmanager hook) to scope
the cycle to the affected resources. Run it alongside the scheduler in `run`, or as
a new `vmware-ai-agent serve` command. Protect it with a bearer token.

**Touches.** `cli.py` (new command / flag), a new `api.py`, `agent.py`
(`analyze_now` already exists and is reused as-is), `config.py` (a `trigger` /
`api` section). **Depends on:** `fastapi`, `uvicorn`.

**Why it's cheap.** `analyze_now()` and the graph are already decoupled from the
scheduler — this is mostly an HTTP shell around an existing method.

---

## 2. Capacity-planning report command  ·  effort: S–M  ·  ✅ implemented

> **Status:** shipped as `vmware-ai-agent capacity` (see [docs/cli.md](docs/cli.md)).
> Logic lives in `reporting/capacity.py`; tests in `tests/test_capacity.py`.

**Problem.** The AriaOps client already wraps `get_capacity_remaining`,
`get_capacity_forecast`, and `get_trend_analysis`, but nothing surfaces them
outside the enrichment step.

**Design.** Add `vmware-ai-agent capacity` that, for a chosen resource kind,
queries forecast + time-to-exhaustion and prints a Rich table (and `--format json`)
sorted by soonest exhaustion. Optionally feed the worst offenders to the LLM for a
short narrative.

**Touches.** `cli.py` (new command), `mcp_clients/ariaops.py` (reuse existing
tools), maybe a small `reporting/capacity.py`. **Depends on:** nothing new.

**Why it's cheap.** Pure read path over tools that already exist and are tested.

---

## 3. Incident history browser & audit trail  ·  effort: M

**Problem.** Every analysis is recorded into the FAISS knowledge base, but there's
no way to browse or search past incidents from the CLI, and remediation actions
aren't persisted anywhere durable.

**Design.** Two parts:
- `vmware-ai-agent history --query "storage latency" --since 30d` over
  `KnowledgeBase.search_similar()` / index metadata, rendered as a table.
- An append-only JSONL **audit log** of every `ActionResult` (action, target,
  status, approver, timestamp) written by `ActionExecutor`, queryable via
  `vmware-ai-agent history --actions`.

**Touches.** `cli.py`, `analysis/knowledge_base.py` (a list/scan helper),
`actions/executor.py` (emit audit records), `config.py` (audit log path).
**Depends on:** nothing new.

**Value.** Compliance/forensics ("what did the agent do, and why") plus
operator trust.

---

## 4. Multi-LLM fallback & cost controls  ·  effort: M

**Problem.** A single `llm` endpoint is a single point of failure; an outage or
rate-limit on the primary kills analysis. There's also no spend guardrail beyond
`max_tokens`.

**Design.** Extend `LLMConfig` with an optional `fallback` endpoint/model. In
`LLMAnalysisEngine._chat_completion`, after retries exhaust on the primary, retry
once against the fallback. Add a per-cycle/day token budget (the engine already
records `tokens_used`) that downgrades to correlation-only output when exceeded,
and expose `vmware_ai_agent_llm_tokens_total` as a metric.

**Touches.** `config.py` (`LLMConfig`), `analysis/llm_engine.py`, `agent.py`
(metric). **Depends on:** nothing new (OpenAI client already supports arbitrary
base URLs).

**Value.** Resilience and predictable cost when using hosted models.

---

## 5. Cost / right-sizing recommendations  ·  effort: M–L

**Problem.** The agent is failure-focused; it doesn't flag *waste*. Idle and
over-provisioned VMs are a common, high-value Ops finding.

**Design.** A new analysis pass that pulls CPU/memory demand vs. allocation via
`get_resource_stats` / `get_trend_analysis`, identifies sustained over-provisioning
or idle resources, and emits `RESOURCE_RECLAIM`/advisory recommendations (the
`ActionType.RESOURCE_RECLAIM` enum value already exists). Surface it in the LLM
prompt and as a `vmware-ai-agent rightsize` report.

**Touches.** `analysis/` (a `rightsizing.py` or prompt section), `mcp_clients/
ariaops.py` (reuse stats tools), `cli.py`. **Depends on:** nothing new.

**Value.** Turns the agent from purely reactive into cost-saving as well.

---

## 6. Custom user-defined patterns from config  ·  effort: S–M  ·  ✅ implemented

> **Status:** shipped via `correlation.custom_patterns_file` (see
> [docs/configuration.md](docs/configuration.md) and
> [docs/patterns.md](docs/patterns.md)). `load_custom_patterns()` in
> `correlation/patterns.py`; tests in `tests/test_custom_patterns.py`.

**Problem.** Adding a `KnownPattern` today means editing `patterns.py` and
redeploying. Operators want to add site-specific patterns without code changes.

**Design.** Allow a `correlation.custom_patterns_file` (YAML) describing extra
patterns; load and validate them at startup and pass the merged list to
`PatternMatcher`. Reuse the existing dataclass shape and regex compilation.

**Touches.** `config.py`, `correlation/patterns.py` (a loader/validator),
`correlation/engine.py` (accept injected patterns), tests. **Depends on:** nothing
new.

**Value.** Self-service extensibility; lowers the barrier to tuning detection.

---

## 7. (Stretch) NSX integration  ·  effort: L

**Problem.** The README documents a multi-vCenter + NSX topology, but there's no
NSX collector or network-partition remediation in code.

**Design.** Add an `NsxMCPClient` (subclass `BaseMCPClient`) or a direct
`NsxCollector`, ingest segment/edge/firewall health, add network-partition and
DFW-related `KnownPattern`s, and map remediation targets to the owning NSX Manager
per the hub-and-spoke contract in the README.

**Touches.** new `mcp_clients/nsx.py` or `collectors/nsx.py`, `correlation/
patterns.py`, `config.py`, `agent.py`, deployment manifests. **Depends on:** an NSX
MCP server or NSX REST access.

**Value.** Closes the gap between the documented topology and the implementation;
extends coverage to the network domain.

---

## Suggested sequencing

1. ~~**#2 Capacity report** and **#6 Custom patterns**~~ — ✅ done (small,
   high-value, no new deps).
2. **#4 Multi-LLM fallback** and **#3 History/audit** — resilience and
   trust/compliance.
3. **#1 Event-driven mode** — unlocks faster response and integrations.
4. **#5 Right-sizing** then **#7 NSX** — larger, domain-expanding efforts.
