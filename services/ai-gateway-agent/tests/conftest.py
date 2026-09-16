from __future__ import annotations

import pytest

from aigw.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        source={"type": "file", "file": {"path": str(tmp_path / "in.json")}},
        enrichment={
            "static_context": {"environment": "test"},
            "redact_fields": ["password"],
            "prompt": {
                "system": "You are a test assistant.",
                "template": "env={{ static.environment }} data={{ record | tojson }}",
            },
        },
        llm_gateway={
            "base_url": "http://gateway.test/v1",
            "model": "test-model",
            "max_retries": 2,
            "backoff_initial_seconds": 0.0,
            "backoff_max_seconds": 0.0,
        },
        sink={"type": "file", "file": {"path": str(tmp_path / "out")}},
        cache={"enabled": False},
        server={"api_key_env": None},
    )


@pytest.fixture
def chat_response() -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "all good"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }
