from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from aigw.models import Record
from aigw.pipeline import Pipeline

URL = "http://gateway.test/v1/chat/completions"


@respx.mock
async def test_end_to_end_file_to_file(settings, chat_response, tmp_path):
    Path(settings.source.file.path).write_text(json.dumps([{"id": "a1", "severity": "high"}]))
    respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response))

    pipeline = Pipeline(settings)
    try:
        results = await pipeline.run_once()
    finally:
        await pipeline.aclose()

    assert [r.status for r in results] == ["ok"]
    assert results[0].result.content == "all good"
    written = Path(results[0].sink_ref)
    assert written.is_file()
    assert json.loads(written.read_text())["result"]["content"] == "all good"


@respx.mock
async def test_redacted_fields_never_reach_the_gateway(settings, chat_response):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response))
    pipeline = Pipeline(settings)
    try:
        await pipeline.process_record(
            Record(payload={"id": "a1", "password": "hunter2"}), publish=False
        )
    finally:
        await pipeline.aclose()

    sent = json.loads(route.calls.last.request.content)
    assert "hunter2" not in json.dumps(sent)


@respx.mock
async def test_gateway_failure_becomes_an_error_result(settings):
    respx.post(URL).mock(return_value=httpx.Response(400, text="nope"))
    pipeline = Pipeline(settings)
    try:
        result = await pipeline.process_record(Record(payload={"id": "a1"}), publish=False)
    finally:
        await pipeline.aclose()

    assert result.status == "error"
    assert "400" in result.error
    assert result.result is None


@respx.mock
async def test_sink_failure_keeps_the_answer(settings, chat_response, monkeypatch):
    respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response))
    pipeline = Pipeline(settings)

    async def boom(_result):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(pipeline.sink, "publish", boom)
    try:
        result = await pipeline.process_record(Record(payload={"id": "a1"}), publish=True)
    finally:
        await pipeline.aclose()

    assert result.status == "sink_error"
    assert result.result.content == "all good"


@respx.mock
async def test_max_records_caps_the_batch(settings, chat_response):
    settings.source.max_records = 1
    Path(settings.source.file.path).write_text(
        json.dumps([{"id": "a1"}, {"id": "a2"}, {"id": "a3"}])
    )
    respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response))
    pipeline = Pipeline(settings)
    try:
        results = await pipeline.run_once()
    finally:
        await pipeline.aclose()
    assert len(results) == 1


@respx.mock
async def test_batch_isolates_failures(settings, chat_response):
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(200, json=chat_response),
            httpx.Response(400, text="nope"),
        ]
    )
    pipeline = Pipeline(settings)
    try:
        results = await pipeline.process_batch(
            [Record(id="a1", payload={}), Record(id="a2", payload={})],
            concurrency=1,
            publish=False,
        )
    finally:
        await pipeline.aclose()
    assert sorted(r.status for r in results) == ["error", "ok"]


@respx.mock
async def test_written_file_records_its_own_location(settings, chat_response):
    respx.post(URL).mock(return_value=httpx.Response(200, json=chat_response))
    pipeline = Pipeline(settings)
    try:
        result = await pipeline.process_record(Record(id="a1", payload={}), publish=True)
    finally:
        await pipeline.aclose()

    stored = json.loads(Path(result.sink_ref).read_text())
    assert stored["sink_ref"] == result.sink_ref
