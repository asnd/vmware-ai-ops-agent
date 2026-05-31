# Deployment

Three supported targets: a single container, the full Docker Compose stack, and
Kubernetes. CI/CD is GitLab.

## Single container

`Dockerfile` (single-stage) and `Containerfile` (multi-stage, Podman-friendly)
both build a `python:3.11-slim` image that runs as a non-root `agent` user,
exposes `9090`, and healthchecks `GET /metrics`.

```bash
docker build -t vmware-ai-ops-agent .

docker run -d \
  -v "$PWD/config:/app/config:ro" \
  -v "$PWD/data:/app/data" \
  -e VMWARE_AI_VROPS__PASSWORD=... \
  -e VMWARE_AI_LLM__API_KEY=... \
  -p 9090:9090 \
  vmware-ai-ops-agent
```

The entrypoint is `vmware-ai-agent`; the default command is
`run --config /app/config/settings.yaml`. Mount `/app/data` to persist the FAISS
knowledge base across restarts.

> Podman: build with `podman build --format docker -f Containerfile` so the
> `HEALTHCHECK` instruction is honoured.

## Docker Compose — full stack

`docker-compose.yaml` brings up the agent plus its dependencies on a shared
`vmware-ai-network` bridge:

| Service | Image / build | Host port | Role |
|---------|---------------|-----------|------|
| `vmware-ai-agent` | built from `Containerfile` | 9090 | the agent (metrics). |
| `ariaops-mcp` | `../ariaops_mcp` | 8080 | Aria Operations MCP server. |
| `entrag-mcp` | `../entrag` | 8081 | EntRAG KB MCP server. |
| `lightllm` | `ghcr.io/modelfamily/lightllm` | 8000 | OpenAI-compatible model serving (GPU). |
| `prometheus` | `prom/prometheus` | 9091 | scrapes the agent. |
| `grafana` | `grafana/grafana` | 3000 | dashboards. |

The agent `depends_on` the two MCP servers and LightLLM being **healthy**, and is
wired to them through `VMWARE_AI_ARIAOPS_MCP__URL` / `VMWARE_AI_ENTRAG_MCP__URL`
and `VMWARE_AI_LLM__ENDPOINT`. Secrets and hosts come from your shell / `.env`
(`VRLI_PASSWORD`, `VCENTER_PASSWORD`, `LLM_API_KEY`, `ARIAOPS_*`, …).

```bash
docker compose up -d
docker compose logs -f vmware-ai-agent
```

`lightllm` reserves an NVIDIA GPU and expects a model under `${MODEL_PATH:-./models}`.
On a CPU-only host, point `VMWARE_AI_LLM__ENDPOINT` at an external
OpenAI-compatible endpoint and don't start the `lightllm` service.

Named volumes persist Prometheus, Grafana, and EntRAG data; bind mounts persist
the agent's `config/`, `data/`, and `logs/`.

## Kubernetes

`deploy/kubernetes/deployment.yaml` is a single multi-document manifest creating:

- a `vmware-ai-ops` **Namespace**;
- a `vmware-credentials` **Secret** (vROps/vRLI/vCenter/LLM/Slack — fill these in);
- a `vmware-ai-agent-config` **ConfigMap** holding `settings.yaml`;
- a **Deployment** (1 replica) running as non-root (uid 1000), with secrets
  injected as `VMWARE_AI_*` env vars, `requests` 512Mi/250m and `limits`
  2Gi/1000m, and liveness/readiness probes on `/metrics`;
- a **ServiceAccount**, a ClusterIP **Service** on 9090, a 10Gi **PVC** for
  `/app/data`, and a Prometheus-Operator **ServiceMonitor**.

```bash
kubectl apply -f deploy/kubernetes/deployment.yaml
kubectl -n vmware-ai-ops edit secret vmware-credentials   # add real values
kubectl -n vmware-ai-ops get pods
```

Point `llm.endpoint` (in the ConfigMap) and the MCP URLs at in-cluster services.
The committed image is `registry.gitlab.com/nikosa/vmware-ai-ops-agent:latest`.

## CI/CD — GitLab

`.gitlab-ci.yml` defines four stages:

1. **lint** — `ruff check`, `ruff format --check`, `mypy` (non-blocking) on `src/` and `tests/`.
2. **test** — `pytest --cov` on Python 3.11, publishing a Cobertura report.
3. **build** — Docker image tagged with the short SHA and `latest`, pushed to the
   GitLab registry (only on `main` / tags).
4. **deploy** — `kubectl apply` to `vmware-ops-dev` and roll the image (manual,
   `main` only).

## Operational checklist

- Persist `data/` (FAISS index + HMAC manifest) so incident history survives
  restarts. See [security.md](security.md).
- Set `knowledge_base.signing_secret` (don't let it fall back to the LLM key).
- Keep `vcenter.dry_run: true` and `agent.auto_remediate.enabled: false` until
  you've reviewed recommendations for a while.
- Scrape `:9090/metrics` and import a Grafana dashboard — see
  [observability.md](observability.md).
