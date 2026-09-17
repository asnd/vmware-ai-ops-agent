from __future__ import annotations

import httpx
import pytest
import respx

from aigw.config import FileSinkConfig, HttpSinkConfig
from aigw.models import ProcessResult
from aigw.sinks import FileSink, HttpSink

URL = "http://reporting.test/insights"


def _result(rid: str = "r1") -> ProcessResult:
    return ProcessResult(id=rid, status="ok")


async def test_file_sink_writes_and_records_its_own_location(tmp_path):
    sink = FileSink(FileSinkConfig(path=str(tmp_path), filename_template="{id}.json"))
    result = _result("r1")
    ref = await sink.publish(result)
    assert ref == str(tmp_path / "r1.json")
    assert result.sink_ref == ref


async def test_bad_filename_template_is_a_runtime_error_not_a_crash(tmp_path):
    # An operator typo — a placeholder outside {id, ts, date} — must fail this
    # one record, not raise something the pipeline's batch gather can't catch.
    sink = FileSink(FileSinkConfig(path=str(tmp_path), filename_template="{nope}.json"))
    with pytest.raises(RuntimeError, match="filename_template is invalid"):
        await sink.publish(_result())


@respx.mock
async def test_http_sink_does_not_retry_a_permanent_400():
    route = respx.post(URL).mock(return_value=httpx.Response(400, text="bad payload"))
    sink = HttpSink(HttpSinkConfig(url=URL, max_retries=3))
    with pytest.raises(RuntimeError, match="http sink failed"):
        await sink.publish(_result())
    assert route.call_count == 1


@respx.mock
async def test_http_sink_retries_a_503_then_succeeds():
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )
    sink = HttpSink(HttpSinkConfig(url=URL, max_retries=3))
    ref = await sink.publish(_result())
    assert ref == URL
    assert route.call_count == 2


@respx.mock
async def test_http_sink_exhausts_retries_on_persistent_503():
    route = respx.post(URL).mock(return_value=httpx.Response(503))
    sink = HttpSink(HttpSinkConfig(url=URL, max_retries=2))
    with pytest.raises(RuntimeError, match="http sink failed"):
        await sink.publish(_result())
    assert route.call_count == 2
