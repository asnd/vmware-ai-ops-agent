from __future__ import annotations

import json

import httpx
import pytest
import respx

from aigw.config import FileSourceConfig, HttpSourceConfig
from aigw.sources import FileSource, HttpSource, dig, to_records


def test_dig_walks_dicts_and_lists():
    assert dig({"a": {"b": [{"c": 1}]}}, "a.b.0.c") == 1
    assert dig({"a": 1}, None) == {"a": 1}


def test_dig_reports_the_missing_segment():
    with pytest.raises(KeyError, match="missing segment 'b'"):
        dig({"a": {}}, "a.b")


def test_to_records_keeps_upstream_ids():
    records = to_records([{"id": "alert-1"}, {"no_id": True}], "test")
    assert records[0].id == "alert-1"
    assert records[1].id != "alert-1"


def test_to_records_wraps_a_bare_dict():
    assert len(to_records({"id": "x"}, "test")) == 1


async def test_file_source_reads_json(tmp_path):
    path = tmp_path / "in.json"
    path.write_text(json.dumps({"items": [{"id": "a"}, {"id": "b"}]}))
    source = FileSource(FileSourceConfig(path=str(path), records_path="items"))
    records = await source.fetch()
    assert [r.id for r in records] == ["a", "b"]
    assert records[0].origin == f"file:{path}"


async def test_file_source_reads_jsonl(tmp_path):
    path = tmp_path / "in.jsonl"
    path.write_text('{"id": "a"}\n\n{"id": "b"}\n')
    records = await FileSource(FileSourceConfig(path=str(path), format="jsonl")).fetch()
    assert [r.id for r in records] == ["a", "b"]


async def test_file_source_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        await FileSource(FileSourceConfig(path=str(tmp_path / "gone.json"))).fetch()


@respx.mock
async def test_http_source_pulls_and_unwraps(monkeypatch):
    monkeypatch.setenv("TOK", "abc")
    route = respx.get("http://svc/api/alerts").mock(
        return_value=httpx.Response(200, json={"data": {"items": [{"id": "a1"}]}})
    )
    cfg = HttpSourceConfig(
        url="http://svc/api/alerts",
        records_path="data.items",
        auth={"type": "bearer", "token_env": "TOK"},
    )
    records = await HttpSource(cfg).fetch()
    assert [r.id for r in records] == ["a1"]
    assert route.calls.last.request.headers["authorization"] == "Bearer abc"


@respx.mock
async def test_http_source_raises_on_error_status():
    respx.get("http://svc/api/alerts").mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await HttpSource(HttpSourceConfig(url="http://svc/api/alerts")).fetch()
