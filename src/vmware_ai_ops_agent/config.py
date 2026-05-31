"""
Configuration management for VMware AI Ops Agent.
"""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class VROpsConfig(BaseModel):
    host: str = "vrops.example.com"
    port: int = 443
    username: str = ""
    password: SecretStr = SecretStr("")
    verify_ssl: bool = True
    timeout: int = 30


class VRLIConfig(BaseModel):
    host: str = "vrli.example.com"
    port: int = 443
    username: str = ""
    password: SecretStr = SecretStr("")
    verify_ssl: bool = True
    timeout: int = 30
    query: dict[str, Any] = Field(
        default_factory=lambda: {
            "default_time_range": "LAST_1_HOUR",
            "max_results": 10000,
        }
    )


class LLMConfig(BaseModel):
    endpoint: str = "http://localhost:8000/v1"
    api_key: SecretStr = SecretStr("")
    model: str = "mistral-7b-instruct"
    max_tokens: int = 4096
    temperature: float = 0.1
    max_retries: int = 3


class VectorDBConfig(BaseModel):
    type: str = "faiss"
    persist_directory: str = "./data/faiss"
    collection_name: str = "vmware_incidents"


class ThresholdsConfig(BaseModel):
    cpu_critical: int = 90
    cpu_warning: int = 80
    memory_critical: int = 95
    memory_warning: int = 85
    disk_latency_critical: int = 50


class AutoRemediateConfig(BaseModel):
    enabled: bool = False
    require_approval: bool = True
    max_actions_per_hour: int = 10
    allowed_actions: list[str] = Field(
        default_factory=lambda: ["vmotion", "drs_rebalance", "snapshot_cleanup"]
    )
    forbidden_actions: list[str] = Field(
        default_factory=lambda: ["vm_power_off", "host_maintenance_mode"]
    )


class AgentConfig(BaseModel):
    cycle_interval: int = 300
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    auto_remediate: AutoRemediateConfig = Field(default_factory=AutoRemediateConfig)


class SlackConfig(BaseModel):
    enabled: bool = False
    webhook_url: SecretStr = SecretStr("")
    channel: str = "#vmware-ops"
    mention_on_critical: str = "@oncall"


class EmailConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = "smtp.example.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    from_address: str = "vmware-ai-agent@example.com"
    recipients: list[str] = Field(default_factory=list)


class ServiceNowConfig(BaseModel):
    enabled: bool = False
    instance: str = "example.service-now.com"
    username: str = ""
    password: SecretStr = SecretStr("")


class NotificationsConfig(BaseModel):
    slack: SlackConfig = Field(default_factory=SlackConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    servicenow: ServiceNowConfig = Field(default_factory=ServiceNowConfig)


class VCenterConfig(BaseModel):
    host: str = "vcenter.example.com"
    port: int = 443
    username: str = ""
    password: SecretStr = SecretStr("")
    verify_ssl: bool = True
    dry_run: bool = True


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "json"
    file: str = "./logs/agent.log"


class MetricsConfig(BaseModel):
    enabled: bool = True
    port: int = 9090
    path: str = "/metrics"


class AriaOpsMCPConfig(BaseModel):
    """Configuration for AriaOps MCP server connection."""

    url: str = "http://localhost:8080/mcp"
    auth_token: SecretStr = SecretStr("")
    timeout: float = 120.0
    enabled: bool = True


class EntragMCPConfig(BaseModel):
    """Configuration for EntRAG MCP server connection."""

    url: str = "http://localhost:8081/mcp"
    auth_token: SecretStr = SecretStr("")
    timeout: float = 60.0
    enabled: bool = True


class KnowledgeBaseConfig(BaseModel):
    runbooks_dir: str = "./config/runbooks"
    kb_cache_dir: str = "./data/kb_cache"
    history_retention: int = 90
    signing_secret: SecretStr = SecretStr("")


class CorrelationConfig(BaseModel):
    """Correlation engine configuration."""

    # Optional YAML file of site-specific patterns merged with the built-ins.
    custom_patterns_file: str = ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VMWARE_AI_", env_nested_delimiter="__", extra="ignore"
    )

    vrops: VROpsConfig = Field(default_factory=VROpsConfig)
    vrli: VRLIConfig = Field(default_factory=VRLIConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    vector_db: VectorDBConfig = Field(default_factory=VectorDBConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    vcenter: VCenterConfig = Field(default_factory=VCenterConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    knowledge_base: KnowledgeBaseConfig = Field(default_factory=KnowledgeBaseConfig)
    correlation: CorrelationConfig = Field(default_factory=CorrelationConfig)
    ariaops_mcp: AriaOpsMCPConfig = Field(default_factory=AriaOpsMCPConfig)
    entrag_mcp: EntragMCPConfig = Field(default_factory=EntragMCPConfig)

    @model_validator(mode="after")
    def _validate_required_secrets(self) -> "Settings":
        checks = [
            (self.vrops.host, "vrops.example.com", self.vrops.password, "VROPS_PASSWORD"),
            (self.vrli.host, "vrli.example.com", self.vrli.password, "VRLI_PASSWORD"),
            (self.vcenter.host, "vcenter.example.com", self.vcenter.password, "VCENTER_PASSWORD"),
            (self.llm.endpoint, "http://localhost:8000/v1", self.llm.api_key, "LLM_API_KEY"),
        ]
        for host, default_host, secret, env_name in checks:
            if host != default_host and not secret.get_secret_value():
                raise ValueError(
                    f"{env_name} is required when {host.split('.')[0] if '.' in host else host} "
                    f"is set to a non-default host ('{host}'). "
                    f"Set the {env_name} environment variable."
                )
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Settings":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {path}")

        with open(path) as f:
            raw_config = yaml.safe_load(f)

        config = cls._expand_env_vars(raw_config)
        return cls(**config)

    @classmethod
    def _expand_env_vars(cls, config: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for key, value in config.items():
            if isinstance(value, dict):
                result[key] = cls._expand_env_vars(value)
            elif isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                env_var = value[2:-1]
                result[key] = os.environ.get(env_var, "")
            else:
                result[key] = value
        return result


def load_settings(config_path: str | Path | None = None) -> Settings:
    if config_path:
        return Settings.from_yaml(config_path)

    search_paths = [
        Path("./config/settings.local.yaml"),
        Path("./config/settings.yaml"),
        Path("/etc/vmware-ai-agent/settings.yaml"),
    ]

    for path in search_paths:
        if path.exists():
            return Settings.from_yaml(path)

    return Settings()
