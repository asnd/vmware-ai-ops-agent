"""Configuration model for the enrichment agent.

Every knob lives in a YAML file (``config/config.yaml`` by default).  Secrets are
never stored in YAML: a field named ``*_env`` holds the *name* of the environment
variable that carries the secret.  Any value can additionally be overridden with
``AIGW__<SECTION>__<FIELD>`` environment variables, which is what the container
deployment uses.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path(os.environ.get("AIGW_CONFIG", "config/config.yaml"))


class HttpAuth(BaseModel):
    """Outbound auth for a source/sink call."""

    type: Literal["none", "bearer", "basic", "header"] = "none"
    token_env: str | None = None
    header_name: str = "Authorization"
    username_env: str | None = None
    password_env: str | None = None

    def headers(self) -> dict[str, str]:
        if self.type == "none":
            return {}
        if self.type == "bearer":
            token = _env(self.token_env)
            return {"Authorization": f"Bearer {token}"} if token else {}
        if self.type == "header":
            token = _env(self.token_env)
            return {self.header_name: token} if token else {}
        if self.type == "basic":
            import base64

            user, password = _env(self.username_env), _env(self.password_env)
            if not user or not password:
                return {}
            blob = base64.b64encode(f"{user}:{password}".encode()).decode()
            return {"Authorization": f"Basic {blob}"}
        return {}


class HttpSourceConfig(BaseModel):
    url: str = ""
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    auth: HttpAuth = Field(default_factory=HttpAuth)
    timeout_seconds: float = 15.0
    # Dotted path into the JSON response that holds the records, e.g. "data.items".
    records_path: str | None = None
    verify_tls: bool = True


class FileSourceConfig(BaseModel):
    path: str = "data/input.json"
    format: Literal["json", "jsonl", "text"] = "json"
    records_path: str | None = None
    encoding: str = "utf-8"


class SourceConfig(BaseModel):
    type: Literal["http", "file"] = "file"
    http: HttpSourceConfig = Field(default_factory=HttpSourceConfig)
    file: FileSourceConfig = Field(default_factory=FileSourceConfig)
    # Cap on records processed per run; 0 means "no limit".
    max_records: int = 0


class LookupConfig(BaseModel):
    """An extra HTTP call whose response is merged into the prompt context."""

    name: str
    url: str
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    auth: HttpAuth = Field(default_factory=HttpAuth)
    timeout_seconds: float = 10.0
    # When true a failing lookup is logged and skipped instead of failing the record.
    optional: bool = True
    records_path: str | None = None
    verify_tls: bool = True


class PromptConfig(BaseModel):
    system: str = "You are a helpful operations assistant. Answer concisely."
    # Jinja2 template rendered with {record, raw, context, static, id, origin} in
    # scope. `record` is filtered by include_fields; `raw` is not, but — like
    # `record` — always has redact_fields masked.
    template: str = "{{ record | tojson(indent=2) }}"
    max_input_chars: int = 24_000


class EnrichmentConfig(BaseModel):
    # Constants merged into the template scope as `static`.
    static_context: dict[str, Any] = Field(default_factory=dict)
    # When set, only these top-level record fields survive into the prompt.
    include_fields: list[str] = Field(default_factory=list)
    # Always stripped from the record before it reaches the LLM.
    redact_fields: list[str] = Field(default_factory=list)
    lookups: list[LookupConfig] = Field(default_factory=list)
    prompt: PromptConfig = Field(default_factory=PromptConfig)


class LLMGatewayConfig(BaseModel):
    """OpenAI-compatible LLM Gateway (LiteLLM, Portkey, Kong AI Gateway, vLLM ...)."""

    base_url: str = "http://localhost:4000/v1"
    chat_path: str = "/chat/completions"
    model: str = "gpt-4o-mini"
    api_key_env: str = "LLM_GATEWAY_API_KEY"
    timeout_seconds: float = 90.0
    max_retries: int = 3
    backoff_initial_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    temperature: float = 0.2
    max_tokens: int = 1024
    # "text" leaves the answer as-is; "json_object" asks the gateway for strict JSON.
    response_format: Literal["text", "json_object"] = "text"
    # Extra headers/body merged verbatim — gateway routing hints, virtual keys, tags.
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    # Response headers copied into the result and the log line. Gateways return
    # their routing decision this way (LiteLLM: x-litellm-model-id, x-litellm-call-id),
    # which is what makes an answer traceable back through the gateway.
    capture_response_headers: list[str] = Field(default_factory=list)
    verify_tls: bool = True

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.chat_path.lstrip('/')}"

    def auth_headers(self) -> dict[str, str]:
        key = _env(self.api_key_env)
        return {"Authorization": f"Bearer {key}"} if key else {}


class FileSinkConfig(BaseModel):
    path: str = "out"
    # Available placeholders: {id}, {ts}, {date}.
    filename_template: str = "{date}/{id}.json"
    append_jsonl: str | None = None


class HttpSinkConfig(BaseModel):
    url: str = ""
    method: Literal["POST", "PUT"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    auth: HttpAuth = Field(default_factory=HttpAuth)
    timeout_seconds: float = 15.0
    max_retries: int = 3
    verify_tls: bool = True


class SinkConfig(BaseModel):
    # "none" is what the synchronous HTTP API uses: the caller *is* the sink.
    type: Literal["none", "file", "http"] = "file"
    file: FileSinkConfig = Field(default_factory=FileSinkConfig)
    http: HttpSinkConfig = Field(default_factory=HttpSinkConfig)


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    # Optional inbound API key; when the env var is unset the API stays open.
    api_key_env: str | None = "AIGW_API_KEY"
    max_body_bytes: int = 1_048_576
    cors_origins: list[str] = Field(default_factory=list)


class SchedulerConfig(BaseModel):
    """Periodic pull mode — the agent polls the source on its own."""

    enabled: bool = False
    interval_seconds: float = 300.0
    jitter_seconds: float = 0.0
    run_on_start: bool = True


class CacheConfig(BaseModel):
    enabled: bool = True
    ttl_seconds: float = 300.0
    max_entries: int = 1000


class ObservabilityConfig(BaseModel):
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    metrics_enabled: bool = True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIGW__",
        env_nested_delimiter="__",
        extra="ignore",
    )

    service_name: str = "ai-gateway-agent"
    source: SourceConfig = Field(default_factory=SourceConfig)
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    llm_gateway: LLMGatewayConfig = Field(default_factory=LLMGatewayConfig)
    sink: SinkConfig = Field(default_factory=SinkConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    @model_validator(mode="after")
    def _check_wiring(self) -> Settings:
        if self.source.type == "http" and not self.source.http.url:
            raise ValueError("source.type=http requires source.http.url")
        if self.sink.type == "http" and not self.sink.http.url:
            raise ValueError("sink.type=http requires sink.http.url")
        return self


def _env(name: str | None) -> str | None:
    return os.environ.get(name) if name else None


class _YamlSource(PydanticBaseSettingsSource):
    """Feeds a parsed YAML mapping into pydantic-settings, ranked below env vars."""

    def __init__(self, settings_cls: type[BaseSettings], data: dict[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data = data

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


def read_yaml(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {}
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping")
    return loaded


def load_settings(path: str | Path | None = None) -> Settings:
    """Read YAML (if present) and let ``AIGW__*`` env vars win over it."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data = read_yaml(config_path)

    class _FromYaml(Settings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            # Priority, highest first: env vars, then the YAML file, then defaults.
            return (
                env_settings,
                dotenv_settings,
                _YamlSource(settings_cls, data),
                file_secret_settings,
            )

    return _FromYaml()
