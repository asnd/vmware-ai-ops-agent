# Observability

The agent ships Prometheus metrics, structured logs, and HTTP health checks.

## Prometheus metrics

When `metrics.enabled` is true, `agent.start()` launches a Prometheus HTTP
server on `metrics.port` (default 9090). Metrics are defined in `agent.py`:

| Metric | Type | Labels | Meaning |
|--------|------|--------|---------|
| `vmware_ai_agent_analysis_cycles_total` | Counter | `status` = `success` \| `partial_error` \| `error` | Cycles completed, by outcome. |
| `vmware_ai_agent_issues_detected_total` | Counter | `severity` | Correlated issues found, by severity. |
| `vmware_ai_agent_cycle_duration_seconds` | Histogram | — | Cycle latency; buckets `5, 10, 30, 60, 120, 300`. |
| `vmware_ai_agent_resource_health` | Gauge | `resource_name`, `resource_kind` | Latest health score per resource. |

The endpoint also serves the standard `process_*` and `python_*` collectors.

### Useful queries

```promql
# Cycle success ratio over 1h
sum(rate(vmware_ai_agent_analysis_cycles_total{status="success"}[1h]))
  / sum(rate(vmware_ai_agent_analysis_cycles_total[1h]))

# p95 cycle duration
histogram_quantile(0.95, sum(rate(vmware_ai_agent_cycle_duration_seconds_bucket[15m])) by (le))

# Critical issues detected per hour
sum(rate(vmware_ai_agent_issues_detected_total{severity="CRITICAL"}[1h]))

# Resources currently below health 50
vmware_ai_agent_resource_health < 50
```

### Suggested alerts

- `analysis_cycles_total{status="error"}` increasing → the cycle is failing
  outright (collection/graph error).
- No increase in `analysis_cycles_total` for `> 3 × cycle_interval` → the agent
  is stalled or down.
- p95 `cycle_duration_seconds` approaching the 120s collection timeout → a slow
  MCP/LLM dependency.

## Scraping

- **Compose:** the bundled `prometheus` service reads
  `deploy/prometheus/prometheus.yml` and is reachable on host port 9091; Grafana
  on 3000 (provisioned from `deploy/grafana/provisioning/`).
- **Kubernetes:** pod annotations (`prometheus.io/scrape: "true"`,
  `prometheus.io/port: "9090"`) and a `ServiceMonitor` are included for the
  Prometheus Operator.

## Logging

`structlog` is configured in two places:

- `cli.py:setup_logging(level)` — used by the CLI; renders **console** output at
  `DEBUG` and **JSON** otherwise.
- `utils/logging.py:setup_logging(level, log_format, log_file)` — the general
  helper honouring `logging.format` (`json`/`console`) and optionally writing to
  `logging.file`.

Both pipelines add log level, logger name, ISO timestamps, and exception/stack
rendering. Every module logs via `structlog.get_logger(__name__)` with
structured key/values, e.g.:

```json
{"event": "Analysis cycle complete", "duration_seconds": 8.3, "level": "info",
 "logger": "vmware_ai_ops_agent.agent", "timestamp": "2026-05-31T12:00:08Z"}
```

Keep `format: json` in production so logs are queryable; use `console` (or
`--log-level DEBUG`) locally.

## Health checks

Both container images and the Kubernetes probes use `GET /metrics` on port 9090
as the liveness/readiness signal — if the metrics server is up, the process is
considered healthy. There is no separate `/health` endpoint on the agent itself
(the MCP servers expose their own `/health`).
