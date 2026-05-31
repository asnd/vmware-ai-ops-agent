# Extending the Agent

Common extension points, each following an existing pattern in the codebase.
After any change, run `pytest`, `ruff check .`, and `ruff format .`.

## Add a correlation pattern

1. Append a `KnownPattern` to `KNOWN_PATTERNS` in
   `correlation/patterns.py`:

   ```python
   KnownPattern(
       id="storage-vmfs-heap",
       name="VMFS Heap Exhaustion",
       category=PatternCategory.STORAGE,
       description="VMFS heap nearing limit",
       severity=Severity.WARNING,
       log_patterns=[r"VMFS3\.HeapSize", r"heap.*exhaust"],
       metric_conditions={"datastore|totalLatency_average": ("gt", 30)},
       alert_names=["VMFS heap"],
       predicted_failure="Datastore operations may stall",
       failure_probability=0.6,
       recommended_actions=["Increase VMFS3.MaxHeapSizeMB", "Reduce open files"],
   )
   ```

2. Use exact vROps stat keys in `metric_conditions` — they must match what
   collection writes into `ResourceHealth.metrics`.
3. Add a case to `tests/test_correlation.py` covering a log/metric/alert that
   should (and shouldn't) fire it.

No engine changes are needed — `PatternMatcher` compiles and evaluates new
patterns automatically. See [patterns.md](patterns.md).

**Without code changes:** operators can add patterns at runtime via
`correlation.custom_patterns_file` (a YAML file). `load_custom_patterns()` in
`correlation/patterns.py` parses it and `VMwareAIOpsAgent._load_patterns()`
merges it with the built-ins — invalid entries are skipped with a warning. See
[patterns.md](patterns.md#adding-your-own-pattern).

## Add a remediation action

1. If it's a new action verb, add a value to `ActionType` in
   `analysis/models.py`.
2. Implement the handler on `ActionExecutor` (`actions/executor.py`) and register
   it in the `self._handlers` map:

   ```python
   self._handlers[ActionType.RESTART_SERVICE] = self._execute_restart_service

   async def _execute_restart_service(self, step, dry_run) -> dict[str, Any]:
       if dry_run:
           return {"dry_run": True, "action": "restart_service", ...}
       ...  # call vCenter / AriaOps client
   ```

3. Decide its safety: only `NOTIFY`/`INVESTIGATE` are in `SAFE_ACTIONS`; anything
   that changes infrastructure should stay non-safe (approval-gated). Add it to
   `allowed_actions` in config to permit it.
4. Cover rate-limit, dedup, and approval paths in `tests/test_executor.py`.

## Add a collector

Model it on `collectors/vrli.py`: an async context manager that authenticates in
`__aenter__`, wraps requests with tenacity retry, and exposes a `collect_all()`
returning your data parsed into the `collectors/models.py` types. Then call it
from `agent.py:_collect_infrastructure_state()` inside the `asyncio.gather`, and
merge its output into the `InfrastructureState`.

## Add an MCP client

Subclass `BaseMCPClient` (`mcp_clients/base.py`) — you get the session handshake,
SSE/JSON decoding, id sequencing, and transport retry for free. Add thin tool
wrappers over `self._call_tool("tool_name", {...})`:

```python
class NsxMCPClient(BaseMCPClient):
    async def list_segments(self, transport_zone: str) -> list[dict]:
        result = await self._call_tool("list_segments", {"tz": transport_zone})
        return result.get("segments", []) if isinstance(result, dict) else []
```

Then add a config model (mirroring `AriaOpsMCPConfig`/`EntragMCPConfig` in
`config.py`), instantiate it in `VMwareAIOpsAgent.__init__` behind an `enabled`
flag, and connect/disconnect it in `start()`/`stop()`. Model tests on
`tests/test_mcp_clients.py` and `tests/test_mcp_base.py` (which use `respx` to
mock HTTP and SSE).

## Add a notification channel

Extend `NotificationService` (`actions/notifications.py`): add a config model to
`NotificationsConfig` (`config.py`), a `_send_<channel>()` coroutine, and dispatch
to it from `notify_issue` / `notify_analysis` behind an `enabled` check — the same
shape as the existing Slack/Email handlers. (ServiceNow is modelled but unwired —
a natural first contribution.)

## Influence the LLM analysis

The prompt and JSON contract live in `analysis/llm_engine.py`
(`SYSTEM_PROMPT` and the `user_prompt` template in `analyze_infrastructure`). To
add a new output field, extend the JSON schema in the prompt, parse it after the
`json.loads`, and add the field to `AnalysisResult` in `analysis/models.py`. Keep
the lenient-parse fallback intact so a malformed response can't crash a cycle.

## Testing conventions

- `pytest` with `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed).
- HTTP is mocked with `respx`; shared fixtures (mock `Settings`, sample
  resources/alerts) live in `tests/conftest.py`.
- Run a focused file: `pytest tests/test_correlation.py -v`.
