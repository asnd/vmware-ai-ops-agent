from __future__ import annotations

import pytest
import yaml

from aigw.config import HttpAuth, Settings, load_settings


def test_yaml_is_loaded(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"service_name": "custom", "llm_gateway": {"model": "from-yaml"}})
    )
    settings = load_settings(path)
    assert settings.service_name == "custom"
    assert settings.llm_gateway.model == "from-yaml"


def test_env_overrides_yaml(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"llm_gateway": {"model": "from-yaml"}}))
    monkeypatch.setenv("AIGW__LLM_GATEWAY__MODEL", "from-env")
    assert load_settings(path).llm_gateway.model == "from-env"


def test_missing_file_falls_back_to_defaults(tmp_path):
    assert load_settings(tmp_path / "nope.yaml").service_name == "ai-gateway-agent"


def test_chat_url_joins_cleanly():
    settings = Settings(llm_gateway={"base_url": "http://gw/v1/", "chat_path": "chat/completions"})
    assert settings.llm_gateway.chat_url == "http://gw/v1/chat/completions"


def test_http_source_requires_url():
    with pytest.raises(ValueError, match="source.http.url"):
        Settings(source={"type": "http"})


def test_bearer_auth_reads_env(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "s3cret")
    auth = HttpAuth(type="bearer", token_env="MY_TOKEN")
    assert auth.headers() == {"Authorization": "Bearer s3cret"}


def test_auth_without_secret_sends_nothing(monkeypatch):
    monkeypatch.delenv("MY_TOKEN", raising=False)
    assert HttpAuth(type="bearer", token_env="MY_TOKEN").headers() == {}
