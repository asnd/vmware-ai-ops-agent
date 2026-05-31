# Module / API Reference

Package root: `src/vmware_ai_ops_agent/`. Everything is async-first.

```
vmware_ai_ops_agent/
├── cli.py              # Typer CLI (see cli.md)
├── config.py           # Pydantic settings (see configuration.md)
├── agent.py            # VMwareAIOpsAgent orchestrator
├── graph.py            # LangGraph workflow (see architecture.md)
├── collectors/         # Infrastructure + log collection and data models
├── mcp_clients/        # MCP transport + AriaOps & EntRAG clients
├── correlation/        # Pattern library + correlation engine (see patterns.md)
├── analysis/           # LLM engine, result models, FAISS knowledge base
├── actions/            # Remediation executor, vCenter client, notifications
└── utils/              # Logging + prompt scrubbing
```

## `agent.py` — `VMwareAIOpsAgent`

The orchestrator. Owns the correlation engine, LLM engine, knowledge base, MCP
clients, scheduler, and the single long-lived `ActionExecutor`.

Key members:

- `start()` / `stop()` — lifecycle: init KB, connect/disconnect MCP clients,
  start metrics server, schedule the cycle, flush KB on shutdown.
- `_run_cycle()` — invokes the graph, updates `AgentState`, emits metrics,
  records the analysis to the KB, fires notifications.
- `_collect_infrastructure_state()` — concurrent AriaOps-MCP + vRLI collection
  with a 120s timeout and empty-list fallbacks.
- `_auto_remediate()` — runs a plan through the executor with per-call vCenter /
  notification clients injected.
- `analyze_now()` — single graph invocation outside the schedule (used by the
  `analyze` CLI command).
- `get_status()` — state snapshot for the `status` command.

`AgentState` (dataclass) tracks `running`, `total_cycles`, `issues_detected`,
`actions_executed`, `last_analysis`, `last_correlation`, and a capped `errors`
list.

## `collectors/`

- **`models.py`** — the shared domain model:
  - Enums: `Severity` (CRITICAL/IMMEDIATE/WARNING/INFO), `ResourceKind`,
    `HealthState` (GREEN→GREY).
  - `ResourceIdentifier`, `Metric` (with `latest_value` / `average` /
    `max_value` properties), `ResourceHealth` (`is_critical()` / `is_warning()`),
    `Symptom`, `Alert` (`is_active()`), `Recommendation`, `LogEntry`
    (`contains_error()`), `LogQueryResult`, `Anomaly`.
  - `InfrastructureState` — the per-cycle snapshot with helpers
    `critical_alerts`, `unhealthy_resources`, `get_resources_by_kind()`,
    `get_alerts_for_resource()`.
- **`vrli.py`** — `VRLICollector`: async context manager, session auth with TTL
  refresh, `query_logs()` / `query_error_logs()`, and `_extract_anomalies()`
  which matches a pre-compiled pattern list and emits per-event (critical) and
  frequency-based (≥10 occurrences) anomalies. `collect_all()` does a single
  fetch reused for both logs and anomalies.
- **`vrops.py`** — `VROpsCollector`: direct REST collector (token auth,
  pagination, retry). Present as a fallback/standalone path; the live agent
  collects Aria Operations data through the MCP client instead.

## `mcp_clients/`

- **`base.py`** — `BaseMCPClient`: MCP Streamable-HTTP transport. Handles the
  `initialize` handshake, `Accept: application/json, text/event-stream`, the
  `MCP-Protocol-Version: 2025-03-26` header, SSE parsing (`_parse_sse_body`),
  JSON-RPC id sequencing, and `_call_tool()` with tenacity retry on transport
  errors only. Async context-manager friendly.
- **`ariaops.py`** — `AriaOpsMCPClient(BaseMCPClient)`. Tool wrappers:
  `list_resources`, `get_resource`, `get_resource_properties`, `list_alerts`,
  `get_alert`, `get_resource_stats`, `get_latest_stats`,
  `get_capacity_remaining`, `get_capacity_forecast`, `get_trend_analysis`, and
  write ops `modify_alerts`, `mark_resources_maintained`,
  `unmark_resources_maintained`. `collect_all()` fans out resource + alert
  queries and parses them into `ResourceHealth` / `Alert` models.
- **`entrag.py`** — `EntragMCPClient(BaseMCPClient)`: `search_kb(query, top_k)`
  (RAG), `search(query)` (returns `title`/`link`/`snippet` dicts for prompt
  context), `get_ingestion_status()`, `get_scrape_status()`.

## `correlation/`

See [patterns.md](patterns.md) for detail.

- **`patterns.py`** — `KnownPattern` dataclass, the `KNOWN_PATTERNS` catalog, and
  `PatternMatcher` (`match_logs` / `match_metrics` / `match_alerts`).
- **`engine.py`** — `CorrelationEngine.correlate()` groups multi-source evidence
  per pattern, computes confidence, folds in anomalies and unhealthy resources,
  and returns a severity-sorted `CorrelationResult` of `CorrelatedIssue`s.

## `analysis/`

- **`models.py`** — `Urgency`, `ActionType`, `PredictedFailure`,
  `RemediationStep`, `RemediationPlan`, `RootCauseAnalysis`, `CorrelatedEvent`,
  and the top-level `AnalysisResult` (with `requires_immediate_action()`,
  `has_predictions()`, `get_high_probability_failures()`).
- **`llm_engine.py`** — `LLMAnalysisEngine`: builds the markdown prompt
  (scrubbed), calls the OpenAI-compatible endpoint with retry, parses the JSON
  response leniently, and builds the `AnalysisResult` + remediation plan. The
  plan is **always** `auto_executable=False`.
- **`knowledge_base.py`** — `KnowledgeBase`: FAISS vector store of past
  `Incident`s with `OpenAIEmbeddings`. Batched writes, HMAC-SHA256 manifest
  integrity over both index files, all FAISS I/O off-loaded via
  `asyncio.to_thread`. Methods: `initialize`, `add_incident`, `flush`,
  `search_similar`, `record_analysis`, `get_statistics`.

## `actions/`

- **`executor.py`** — `ActionExecutor.execute_plan()`: allow/forbid filtering,
  per-hour rate limit, 30-minute dedup window, human-approval gating, and a
  handler map per `ActionType`. `SAFE_ACTIONS = {NOTIFY, INVESTIGATE}`. Returns
  an `ExecutionResult` of per-step `ActionResult`s.
- **`vcenter.py`** — `VCenterClient`: session auth, `vmotion_vm`,
  `storage_vmotion_vm`, `trigger_drs_recommendation`, `find_best_target_host`,
  `find_best_target_datastore`. Honours `dry_run`.
- **`notifications.py`** — `NotificationService`: `notify_issue` (Slack + Email),
  `notify_analysis` (Slack, critical/high only), with SMTP send off the event
  loop.

## `utils/`

- **`logging.py`** — `setup_logging(level, log_format, log_file)` and
  `get_logger(name)`.
- **`security.py`** — `scrub_sensitive_data(text)`: redacts IPs, emails, UUIDs,
  JWTs, base64 auth headers, and `key: value` secret patterns before any text
  reaches the LLM.
