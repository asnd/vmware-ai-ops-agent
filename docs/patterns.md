# Correlation Patterns

The correlation layer turns raw infrastructure state into a ranked list of
`CorrelatedIssue`s by matching against a library of `KnownPattern`s and folding
in anomalies and unhealthy resources. Source: `src/vmware_ai_ops_agent/correlation/`.

## The `KnownPattern` schema

`patterns.py:KnownPattern` (dataclass):

| Field | Meaning |
|-------|---------|
| `id`, `name`, `description` | Identity / human label. |
| `category` | `PatternCategory`: storage / network / compute / memory / availability. |
| `severity` | A `Severity` enum value. |
| `log_patterns` | Regexes matched (case-insensitive) against `LogEntry.text`. |
| `metric_conditions` | `{stat_key: (operator, threshold)}` where operator is `gt`/`lt`, evaluated against `ResourceHealth.metrics[stat_key].latest_value`. |
| `alert_names` | Substrings matched against `Alert.name` (case-insensitive). |
| `predicted_failure` | Hypothesised impact, fed into the issue's root-cause/impact fields. |
| `failure_probability` | Prior likelihood (0–1). |
| `recommended_actions` | Human guidance carried onto the issue. |
| `auto_remediate` | Hint that the pattern is a candidate for automation (advisory only). |

## Built-in catalog (`KNOWN_PATTERNS`)

| id | name | category | severity | matches on | prob |
|----|------|----------|----------|-----------|------|
| `storage-apd` | All Paths Down (APD) | storage | CRITICAL | logs + alerts | 0.95 |
| `storage-pdl` | Permanent Device Loss (PDL) | storage | CRITICAL | logs + alerts | 0.99 |
| `storage-latency-high` | High Storage Latency | storage | WARNING | logs + `datastore\|totalLatency_average > 20` | 0.7 |
| `memory-pressure` | Memory Pressure | memory | WARNING | logs + `mem\|usage_average > 90` | 0.6 |
| `memory-oom` | Out of Memory | memory | CRITICAL | logs + `mem\|usage_average > 98` | 0.9 |
| `network-link-down` | Network Link Down | network | CRITICAL | logs + alerts | 0.95 |
| `network-dvport-blocked` | DVPort Blocked | network | WARNING | logs | 0.8 |
| `compute-cpu-contention` | CPU Contention | compute | WARNING | `cpu\|ready_summation > 5000` + `cpu\|usage_average > 85` | 0.6 |
| `ha-failover` | HA Failover Event | availability | CRITICAL | logs | 0.3 |
| `capacity-datastore-full` | Datastore Space Low | storage | WARNING | logs + `diskspace\|used_average > 85` | 0.7 |

`memory-pressure`, `compute-cpu-contention`, and `capacity-datastore-full` carry
`auto_remediate=True` as an advisory flag.

> The metric keys (e.g. `mem|usage_average`) are vROps stat keys. For a condition
> to fire, collection must populate `ResourceHealth.metrics` with that exact key.

## Matching — `PatternMatcher`

Compiles every `log_patterns` regex once at construction, then offers three
independent matchers:

- `match_logs(logs)` → patterns whose regexes hit any `LogEntry.text`.
- `match_metrics(resources)` → patterns whose `metric_conditions` are satisfied
  by a resource's latest metric values.
- `match_alerts(alerts)` → patterns whose `alert_names` substring-match an alert.

Each returns `(pattern, [evidence])` tuples.

## Correlating — `CorrelationEngine.correlate()`

1. Run all three matchers over the `InfrastructureState`.
2. Group evidence per `pattern.id` (logs + resources + alerts).
3. For each pattern with evidence, build a `CorrelatedIssue` with confidence:

   ```
   source_count = number of evidence types present (logs / resources / alerts)
   confidence   = min(source_count * 0.3 + 0.2, 0.95)
   ```

   So a single-source hit ≈ 0.5; multi-source corroboration pushes it toward the
   0.95 cap. `first_detected` / `last_updated` are derived from log and alert
   timestamps.
4. `_correlate_anomalies()` attaches vRLI/metric anomalies to matching issues
   (boosting confidence by 0.1, capped at 0.99) and promotes unmatched
   **critical** anomalies into standalone issues.
5. `_correlate_unhealthy_resources()` raises a CRITICAL issue (confidence 0.8)
   for any resource in a red/critical health state that no pattern already
   covers.
6. Issues are sorted by severity then descending confidence.

The resulting `CorrelationResult` exposes `critical_issues` and
`high_confidence_issues` (≥ 0.7) and is what gates the `enrich`/`analyze` nodes
(see [architecture.md](architecture.md)).

## Adding your own pattern

Two options:

- **In code** — append a `KnownPattern` to `KNOWN_PATTERNS` (or pass a custom
  list to `PatternMatcher`).
- **In config (no code change)** — point `correlation.custom_patterns_file` at a
  YAML file; `load_custom_patterns()` parses it and the agent merges it with the
  built-ins at startup:

  ```yaml
  patterns:
    - id: storage-vmfs-heap
      name: VMFS Heap Exhaustion
      category: storage          # storage|network|compute|memory|availability
      severity: WARNING          # CRITICAL|IMMEDIATE|WARNING|INFO
      log_patterns: ["VMFS heap exhausted"]
      metric_conditions:
        "mem|usage_average": ["gt", 99]   # [operator, threshold]
      alert_names: ["VMFS heap"]
      predicted_failure: Datastore operations stall
      failure_probability: 0.6
      recommended_actions: ["Increase VMFS3.MaxHeapSizeMB"]
      auto_remediate: false
  ```

Step-by-step guidance and tests are in
[extending.md](extending.md#add-a-correlation-pattern).
