# ai-gateway-agent

A standalone microservice that sits between a data producer and an LLM Gateway:

```
 source                      this service                        LLM Gateway        sink
┌──────────────┐      ┌───────────────────────────────┐      ┌───────────────┐   ┌──────────┐
│ sibling API  │ ───► │ fetch → enrich → prompt → call │ ───► │ LiteLLM /     │   │ file     │
│ or JSON file │      │        (Content Enricher)      │ ◄─── │ Portkey / ... │   │ webhook  │
└──────────────┘      └───────────────────────────────┘      └───────────────┘   │ HTTP resp│
                                    │                                             └──────────┘
                                    └── /v1/process, /v1/run, /metrics, /healthz
```

It does not talk to model providers directly. It targets any **OpenAI-compatible
`/chat/completions` endpoint**, which is what every LLM gateway exposes, so the
gateway keeps owning keys, routing, budgets, rate limits and audit.

## What pattern is this?

It is the **Content Enricher** from the Enterprise Integration Patterns catalogue
(also called Data Enricher), applied to LLM traffic: a message arrives with less
context than the consumer needs, the enricher looks up the missing context and
forwards a richer message downstream. Around that core it composes a few more
well-known patterns:

| Concern in this service | Pattern |
| --- | --- |
| `sources/` → `enrich` → `llm` → `sinks` as replaceable stages | Pipes and Filters |
| One HTTP call fans out into source + N context lookups | Gateway Aggregation |
| Upstream JSON is normalised into `Record`, answers into `ProcessResult` | Message Translator / Anti-Corruption Layer |
| Prompt assembled from a template plus an invisible system prompt | Prompt enrichment / prompt decoration |
| Batch pull on a timer, or on-demand via HTTP | Polling Consumer + Request-Reply |

What it is **not**: it is not itself an AI gateway. A gateway is a horizontal,
domain-agnostic proxy (keys, routing, quotas, caching, guardrails). This service
is the vertical, domain-aware step in front of it. Keeping the two separate is
the point — the domain logic here stays small, and the operational concerns stay
in a component built for them.

## Do you need to build it at all?

Four honest alternatives, three of them without writing code:

1. **Do the enrichment inside the gateway.** If all you need is "prepend a system
   prompt / fill a template", gateways already do this as configuration:
   Kong's `ai-prompt-decorator` and `ai-prompt-template`, APISIX's plugins of the
   same names, kgateway/agentgateway prompt enrichment. No new service, no new
   deployment. This stops being enough as soon as enrichment means *calling other
   services* and reshaping their output.
2. **Use a low-code orchestrator.** n8n (Schedule/Webhook trigger → HTTP Request →
   Code → LLM → file) or Dify (workflow published as a REST endpoint in one click)
   cover this exact shape with zero code. The cost is a stateful platform with its
   own database and credential store, and a workflow that lives as a JSON blob
   rather than something reviewable in a diff.
3. **Use a pipeline framework and just deploy it.** Haystack + Hayhooks, LangChain
   + LangServe, LlamaIndex Workflows, BentoML, Ray Serve — each turns a pipeline
   into a REST service. Right answer when the pipeline is about to grow branches,
   RAG, or agent loops; heavier than needed for fetch → template → call → write.
4. **Build a small service like this one.** Justified when the enrichment is
   domain-specific, the dependencies are your own services, redaction before the
   prompt is a control you want under test, and you want a plain container in the
   stack the rest of the repository already uses.

This repository is option 4, chosen deliberately over the others and kept thin
enough to throw away: 1172 lines of logic, 445 of tests. If the workflow ends up
being edited by non-developers, or there are ten of these rather than one, option
2 wins and this service should be retired rather than grown.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env            # put LLM_GATEWAY_API_KEY here
cp config/config.example.yaml config/config.yaml

aigw validate                   # check config + prompt template
aigw run-once --stdout          # file in → gateway → file out
aigw serve                      # HTTP API on :8080
aigw worker                     # periodic pull loop, no HTTP server
```

```bash
docker compose up -d            # agent + a LiteLLM gateway
```

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/process` | Enrich and answer. Body may carry `payload`, `records`, or nothing (then the configured source is pulled). |
| `POST` | `/v1/run` | Run one full pull → enrich → answer → **publish to sink** cycle. |
| `GET` | `/v1/config` | Effective configuration (secrets appear only as env-var names). |
| `GET` | `/healthz`, `/readyz` | Liveness and readiness. `/readyz` does not call the gateway. |
| `GET` | `/metrics` | Prometheus exposition. |
| `GET` | `/docs` | OpenAPI UI. |

```bash
curl -s localhost:8080/v1/process \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $AIGW_API_KEY" \
  -d '{"payload": {"id": "alert-1", "severity": "critical",
                   "message": "CPU contention on esxi-07"}}' | jq
```

Per-request overrides: `model`, `system_prompt`, `static_context`, `publish`
(also write to the sink), `bypass_cache`.

## Working with LiteLLM

The gateway in `docker-compose.yaml` is LiteLLM, and `deploy/litellm.yaml` is a
working reference rather than a placeholder. What matters at the seam:

- **`llm_gateway.model` is a LiteLLM alias**, a `model_name` from its `model_list`
  (`ops-analysis`), never a provider model id. Changing which model actually
  answers is then a gateway edit, and this service never learns a provider name.
- **Retries exist on both sides and do different jobs.** LiteLLM's
  `router_settings.num_retries` and `fallbacks` handle provider-side faults before
  a response comes back; this service's `max_retries` covers the hop to the gateway
  itself. Keep both modest — they multiply.
- **Spend attribution is config, not code.** `extra_headers['x-litellm-tags']` tags
  the request, and `extra_body.metadata.spend_logs_metadata` rides along into the
  spend log, so a cost line can be traced back to a run.
- **Virtual keys need a database.** With `master_key` alone the proxy routes fine,
  but per-key budgets and virtual keys cannot be resolved or metered.
- **`capture_response_headers`** copies LiteLLM's routing decision
  (`x-litellm-model-id`, `x-litellm-call-id`) into the result and the log line —
  this is what lets you find the same call in the gateway's own logs when an
  answer looks wrong. It is opt-in and vendor-neutral: name whatever headers your
  gateway returns.
- **Health probes differ.** LiteLLM exposes `/health/liveliness` and
  `/health/readiness` on its own port; this service's `/readyz` reports its wiring
  and deliberately does not call the gateway, so a gateway outage does not take
  the agent's readiness down with it.

## Configuration

Everything lives in `config/config.yaml` (`AIGW_CONFIG` picks another path);
`config/config.example.yaml` is the annotated reference. Two rules:

- **Secrets never go in YAML.** A field named `*_env` holds the *name* of the
  environment variable that carries the secret.
- **Env vars outrank the file**: `AIGW__LLM_GATEWAY__MODEL=gpt-4o` wins over
  `llm_gateway.model`, which is how the container deployment overrides things.

| Section | What it controls |
| --- | --- |
| `source` | `http` (pull a sibling service) or `file` (json / jsonl / text), `records_path`, `max_records` |
| `enrichment` | `static_context`, `include_fields`, `redact_fields`, `lookups`, Jinja2 `prompt.system` / `prompt.template` |
| `llm_gateway` | `base_url`, `model`, `api_key_env`, retries/backoff, `response_format`, `extra_headers`/`extra_body`, `capture_response_headers` |
| `sink` | `file` (atomic write + optional JSONL append), `http` (webhook with retries), or `none` |
| `server` | bind address, optional inbound `api_key_env`, CORS |
| `scheduler` | periodic pull: `interval_seconds`, `jitter_seconds` |
| `cache` | in-process TTL cache keyed by model + messages |

The prompt template renders with `record` (post-redaction), `raw`, `context`
(lookup results, keyed by lookup name), `static`, `id`, `origin`. Undefined
variables raise instead of silently rendering empty.

## Operational behaviour

- **Retries**: the gateway client retries 408/409/425/429 and 5xx with exponential
  backoff; other 4xx fail fast, because they are our bug, not the gateway's.
- **Failure isolation**: one bad record yields `status: "error"` in the batch and
  never aborts the run.
- **Sink failures keep the answer**: delivery problems surface as
  `status: "sink_error"` with the LLM result still attached.
- **Redaction happens before templating**, so `redact_fields` values cannot reach
  the gateway.
- **Metrics**: `aigw_records_total`, `aigw_llm_requests_total`,
  `aigw_llm_latency_seconds`, `aigw_llm_tokens_total`, `aigw_cache_total`,
  `aigw_sink_total`, `aigw_pipeline_latency_seconds`.
- **Logs**: structlog, JSON by default, every line carries the record id.
- **Traceability**: captured gateway headers land in `result.gateway_meta`, so a
  stored answer points at the gateway call that produced it.

## Tests

```bash
pytest -q          # 50 tests, gateway and sibling services mocked with respx
ruff check . && ruff format --check .
mypy src/
```

CI runs exactly these, plus `aigw validate` against both shipped configs and a
Docker build, in `.github/workflows/ai-gateway-agent.yml`. That workflow is
scoped by path to this directory, so it is independent of the root package's
`ci.yml` and neither triggers the other.

## Layout

```
src/aigw/
  config.py      YAML + env settings model
  models.py      Record, EnrichedRecord, LLMResult, ProcessResult
  sources.py     file and http input adapters
  enrich.py      redaction, context lookups, prompt rendering
  llm.py         OpenAI-compatible gateway client, retries, TTL cache
  sinks.py       file and webhook output adapters
  pipeline.py    the orchestrator
  api.py         FastAPI app
  scheduler.py   periodic pull loop
  cli.py         serve / run-once / worker / validate
```
