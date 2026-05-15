"""
Tests for configuration management.
"""

import os
import tempfile

import pytest

from vmware_ai_ops_agent.config import (
    AutoRemediateConfig,
    LLMConfig,
    Settings,
    VROpsConfig,
    load_settings,
)


class TestSettings:
    """Test suite for Settings configuration."""

    def test_default_settings(self):
        """Default settings should be valid."""
        settings = Settings()

        assert settings.vrops.port == 443
        assert settings.vrli.port == 443
        assert settings.agent.cycle_interval == 300
        assert settings.metrics.enabled is True

    def test_vrops_config_defaults(self):
        """vROps config should have sensible defaults."""
        config = VROpsConfig()

        assert config.verify_ssl is True
        assert config.timeout == 30

    def test_auto_remediate_defaults(self):
        """Auto-remediation should be disabled by default with safety guards."""
        config = AutoRemediateConfig()

        assert config.enabled is False
        assert config.require_approval is True
        assert "vm_power_off" in config.forbidden_actions
        assert "vmotion" in config.allowed_actions

    def test_llm_config_defaults(self):
        """LLM config should have reasonable defaults."""
        config = LLMConfig()

        assert config.temperature == 0.1
        assert config.max_tokens == 4096
        assert config.max_retries == 3

    def test_settings_from_yaml(self):
        """Settings should load from YAML file."""
        yaml_content = """
vrops:
  host: test-vrops.local
  port: 443
  username: admin
  password: secret123

agent:
  cycle_interval: 120
  auto_remediate:
    enabled: true
    require_approval: false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()

            try:
                settings = Settings.from_yaml(f.name)

                assert settings.vrops.host == "test-vrops.local"
                assert settings.vrops.username == "admin"
                assert settings.agent.cycle_interval == 120
                assert settings.agent.auto_remediate.enabled is True
                assert settings.agent.auto_remediate.require_approval is False
            finally:
                os.unlink(f.name)

    def test_settings_env_var_expansion(self):
        """Settings should expand environment variables."""
        os.environ["TEST_VROPS_HOST"] = "env-vrops.local"
        os.environ["TEST_VROPS_PASS"] = "env-secret"

        yaml_content = """
vrops:
  host: ${TEST_VROPS_HOST}
  password: ${TEST_VROPS_PASS}
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()

            try:
                settings = Settings.from_yaml(f.name)

                assert settings.vrops.host == "env-vrops.local"
                assert settings.vrops.password.get_secret_value() == "env-secret"
            finally:
                os.unlink(f.name)
                del os.environ["TEST_VROPS_HOST"]
                del os.environ["TEST_VROPS_PASS"]

    def test_settings_file_not_found(self):
        """Should raise error for missing config file."""
        with pytest.raises(FileNotFoundError):
            Settings.from_yaml("/nonexistent/path/config.yaml")

    def test_load_settings_default(self):
        """load_settings should return defaults when no config found."""
        settings = load_settings(None)
        assert isinstance(settings, Settings)

    def test_thresholds_config(self):
        """Threshold configuration should be accessible."""
        settings = Settings()

        assert settings.agent.thresholds.cpu_critical == 90
        assert settings.agent.thresholds.cpu_warning == 80
        assert settings.agent.thresholds.memory_critical == 95

    def test_mcp_config_defaults(self):
        """MCP configuration should have sensible defaults."""
        settings = Settings()

        assert settings.ariaops_mcp.url == "http://localhost:8080/mcp"
        assert settings.ariaops_mcp.enabled is True
        assert settings.ariaops_mcp.timeout == 120.0

        assert settings.entrag_mcp.url == "http://localhost:8081/mcp"
        assert settings.entrag_mcp.enabled is True
        assert settings.entrag_mcp.timeout == 60.0

    def test_mcp_config_from_yaml(self):
        """MCP configuration should load from YAML."""
        yaml_content = """
ariaops_mcp:
  url: http://ariaops:8080/mcp
  enabled: true
  timeout: 90.0

entrag_mcp:
  url: http://entrag:8081/mcp
  enabled: false
  timeout: 45.0
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()

            try:
                settings = Settings.from_yaml(f.name)

                assert settings.ariaops_mcp.url == "http://ariaops:8080/mcp"
                assert settings.ariaops_mcp.timeout == 90.0
                assert settings.entrag_mcp.enabled is False
                assert settings.entrag_mcp.timeout == 45.0
            finally:
                os.unlink(f.name)


class TestSecretHandling:
    """Test suite for secret/password handling."""

    def test_password_not_exposed(self):
        """Passwords should not be exposed in string representation."""
        config = VROpsConfig(password="supersecret")

        str_repr = str(config)
        assert "supersecret" not in str_repr

    def test_password_accessible_via_method(self):
        """Passwords should be accessible via get_secret_value()."""
        config = VROpsConfig(password="supersecret")

        assert config.password.get_secret_value() == "supersecret"
