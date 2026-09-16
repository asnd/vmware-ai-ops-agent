from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from aigw.api import create_app

URL = "http://gateway.test/v1/chat/completions"


def test_health_and_ready(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        ready = client.get("/readyz").json()
        assert ready["llm_gateway"] == URL
        assert ready["source"] == "file"


def test_metrics_are_exposed(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert "aigw_records_total" in response.text


@respx.mock
def test_process_inline_payload(settings, chat_response):
    respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response))
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/process", json={"payload": {"id": "a1"}})

    body = response.json()
    assert response.status_code == 200
    assert body["count"] == 1 and body["succeeded"] == 1
    assert body["results"][0]["result"]["content"] == "all good"
    # publish defaults to false: the caller is the consumer.
    assert body["results"][0]["sink_ref"] is None


@respx.mock
def test_process_falls_back_to_the_source(settings, chat_response):
    Path(settings.source.file.path).write_text(json.dumps([{"id": "a1"}, {"id": "a2"}]))
    respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response))
    with TestClient(create_app(settings)) as client:
        body = client.post("/v1/process", json={}).json()
    assert body["count"] == 2


def test_source_failure_is_a_502(settings):
    # The configured source file does not exist in this test.
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/process", json={})
    assert response.status_code == 502
    assert "source failed" in response.json()["detail"]


@respx.mock
def test_run_endpoint_publishes(settings, chat_response):
    Path(settings.source.file.path).write_text(json.dumps([{"id": "a1"}]))
    respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response))
    with TestClient(create_app(settings)) as client:
        body = client.post("/v1/run").json()
    assert body["succeeded"] == 1
    assert Path(body["results"][0]["sink_ref"]).is_file()


def test_api_key_is_enforced_when_configured(settings, monkeypatch):
    settings.server.api_key_env = "AIGW_TEST_KEY"
    monkeypatch.setenv("AIGW_TEST_KEY", "letmein")
    with TestClient(create_app(settings)) as client:
        assert client.post("/v1/process", json={"payload": {}}).status_code == 401
        assert client.get("/healthz").status_code == 200
        blocked = client.post("/v1/process", json={"payload": {}}, headers={"X-API-Key": "wrong"})
        assert blocked.status_code == 401


def test_config_endpoint_exposes_only_env_var_names(settings):
    with TestClient(create_app(settings)) as client:
        body = client.get("/v1/config").json()
    assert body["llm_gateway"]["api_key_env"] == "LLM_GATEWAY_API_KEY"
    assert "api_key" not in body["llm_gateway"]
