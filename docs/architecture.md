# Architecture

The agent runs a repeating **analysis cycle**. Each cycle is a single pass
through a [LangGraph](https://github.com/langchain-ai/langgraph) state machine
that collects infrastructure state, correlates it, enriches it with external
knowledge, asks an LLM for analysis, and (optionally) remediates.

## High-level data flow

```
                         APScheduler (every cycle_interval seconds)
                                        │
                                        ▼
                          VMwareAIOpsAgent._run_cycle()
                                        │  graph.ainvoke(initial_state)
                                        ▼
   ┌───────────┐   ┌────────────┐   ┌──────────┐   ┌──────────┐   ┌───────────┐
   │  collect  │──▶│ correlate  │──▶│  enrich  │──▶│ analyze  │──▶│ remediate │──▶ END
   └─────┬─────┘   └─────┬──────┘   └────┬─────┘   └────┬─────┘   └─────┬─────┘
         │               │               │              │               │
   AriaOps MCP +     KnownPattern    EntRAG MCP +    LLM (OpenAI-     ActionExecutor →
   vRLI collector    matching +      FAISS KB +      compatible)      vCenter / AriaOps
                     CorrelationEngine AriaOps capacity                 write ops
```

Conditional edges short-circuit the graph:

- After **correlate**, `should_enrich()` routes to `END` when there are no
  correlated issues (nothing worth analysing).
- After **analyze**, `should_remediate()` routes to `END` unless the analysis
  produced a remediation plan flagged `auto_executable` *and* no errors were
  recorded.

Source: `src/vmware_ai_ops_agent/graph.py`, assembled in
`create_agent_graph()` and invoked from `agent.py:_run_cycle()`.

## The graph nodes

| Node | Function | Responsibility |
|------|----------|----------------|
| `collect` | `collect_node` | Calls `collector_func()` → an `InfrastructureState`. |
| `correlate` | `correlate_node` | Runs `CorrelationEngine.correlate()` → a `CorrelationResult`. |
| `enrich` | `enrich_node` | In parallel: EntRAG KB search + local FAISS similarity + AriaOps capacity for affected resources. |
| `analyze` | `analyze_node` | Formats enrichment context and calls `LLMAnalysisEngine.analyze_infrastructure()`. |
| `remediate` | `remediate_node` | Calls the remediation wrapper, which runs the `ActionExecutor`. |

Graph state is a `TypedDict` (`graph.py:AgentState`) with keys
`infrastructure_state`, `correlation_result`, `analysis_result`, `kb_results`,
`search_results`, `capacity_data`, `remediation_status`, and `errors`. Each node
returns a partial dict that LangGraph merges into the running state. Nodes catch
their own exceptions and append to `errors` so a single failure degrades the
cycle rather than crashing it.

## Collection: MCP-first with fallback

`agent.py:_collect_infrastructure_state()` gathers two sources concurrently
(`asyncio.gather`, 120s overall timeout):

1. **Aria Operations** via `AriaOpsMCPClient.collect_all()` — resources + active
   alerts. If the MCP client is disabled or the call fails, it yields empty lists
   (the cycle continues with whatever else it has).
2. **vRLI** via `VRLICollector.collect_all()` — recent error/warning logs plus
   log-pattern anomalies.

The results populate an `InfrastructureState`
(`collectors/models.py`). A direct `VROpsCollector` (`collectors/vrops.py`)
still exists in the tree for REST-based collection, but the live path uses the
AriaOps MCP client.

## Composition over inheritance: MCP clients

Rather than embedding vendor SDKs, the agent **composes** two MCP servers over a
shared transport:

- `AriaOpsMCPClient` (`mcp_clients/ariaops.py`) — wraps Aria Operations tools:
  list resources/alerts, stats, capacity/forecast/trend, and opt-in write
  operations (`modify_alerts`, `mark_resources_maintained`).
- `EntragMCPClient` (`mcp_clients/entrag.py`) — wraps the EntRAG RAG server:
  `rag_query`, `ingestion_status`, `scrape_status`.

Both subclass `BaseMCPClient` (`mcp_clients/base.py`), which owns the MCP
Streamable-HTTP session lifecycle: the `initialize` handshake, `Accept:
application/json, text/event-stream` negotiation, SSE frame parsing, JSON-RPC id
sequencing, and tenacity retry on *transport* errors only (MCP-level
`RuntimeError`s are surfaced, not retried). See [modules.md](modules.md#mcp_clients)
and [security.md](security.md).

Clients are created in `VMwareAIOpsAgent.__init__` (only when enabled in config),
connected in `start()`, and disconnected in `stop()`. If a connect fails, the
client is set to `None` and the agent degrades to its fallbacks.

## Analysis and the knowledge loop

`LLMAnalysisEngine` (`analysis/llm_engine.py`) formats the worst-health
resources, severity-sorted alerts, and most-recent logs into a markdown prompt,
injects the enrichment context, and asks the LLM for a JSON response. The JSON is
parsed leniently (and falls back to free text) into an `AnalysisResult`
(`analysis/models.py`) carrying a summary, urgency, predicted failures, root
cause, optional remediation plan, token usage, and timing.

Every completed analysis is recorded as an `Incident` in the FAISS-backed
`KnowledgeBase` (`analysis/knowledge_base.py`). Those incidents are what the
`enrich` node retrieves on later cycles — a closing feedback loop where past
analyses inform future ones.

## Remediation (guarded)

If a plan is produced, `agent.py:_auto_remediate()` runs it through a single,
long-lived `ActionExecutor` (`actions/executor.py`) whose rate-limit and dedup
state persists across cycles. The executor enforces an allow/forbid list,
per-hour rate limiting, a 30-minute dedup window, and human-approval gating for
every non-safe action. vCenter and notification clients are injected per call.

> **Safety default:** the LLM engine always sets `auto_executable = False` and
> marks every step `requires_approval = True`, and `auto_remediate.enabled` is
> `false` by default. Out of the box the agent **observes and recommends**; it
> does not change infrastructure until you deliberately enable it. See
> [security.md](security.md#remediation-guardrails).

## Cross-cutting concerns

- **Scheduling:** `AsyncIOScheduler` with a single non-overlapping interval job
  (`max_instances=1`). The first cycle runs immediately on `start()`.
- **Metrics:** Prometheus counters/histogram/gauge exported on a dedicated HTTP
  port. See [observability.md](observability.md).
- **Logging:** `structlog` with JSON or console rendering. See
  [observability.md](observability.md).
- **Security:** prompt scrubbing before LLM calls, `SecretStr` config fields, and
  HMAC-signed FAISS index. See [security.md](security.md).
