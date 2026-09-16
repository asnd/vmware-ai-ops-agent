from __future__ import annotations

import httpx
import pytest
import respx

from aigw.config import EnrichmentConfig
from aigw.enrich import REDACTED, Enricher, project
from aigw.models import Record


def test_project_redacts_and_filters():
    cfg = EnrichmentConfig(include_fields=["a", "password"], redact_fields=["password"])
    out = project({"a": 1, "b": 2, "password": "hunter2"}, cfg)
    assert out == {"a": 1, "password": REDACTED}


async def test_enrich_renders_the_prompt():
    cfg = EnrichmentConfig(
        static_context={"environment": "prod"},
        prompt={"system": "sys", "template": "{{ static.environment }}:{{ record.id }}"},
    )
    enriched = await Enricher(cfg).enrich(Record(id="r1", payload={"id": "r1"}))
    assert enriched.user_prompt == "prod:r1"
    assert enriched.system_prompt == "sys"


async def test_enrich_honours_overrides():
    cfg = EnrichmentConfig(
        static_context={"environment": "prod"},
        prompt={"system": "sys", "template": "{{ static.environment }}"},
    )
    enriched = await Enricher(cfg).enrich(
        Record(payload={}), static_override={"environment": "dr"}, system_override="other"
    )
    assert enriched.user_prompt == "dr"
    assert enriched.system_prompt == "other"


async def test_enrich_truncates_long_prompts():
    cfg = EnrichmentConfig(prompt={"template": "{{ record.blob }}", "max_input_chars": 10})
    enriched = await Enricher(cfg).enrich(Record(payload={"blob": "x" * 100}))
    assert enriched.truncated
    assert enriched.user_prompt.startswith("x" * 10)
    assert enriched.user_prompt.endswith("[truncated]")


async def test_unknown_template_variable_is_an_error():
    cfg = EnrichmentConfig(prompt={"template": "{{ nope.field }}"})
    with pytest.raises(ValueError, match="prompt template error"):
        await Enricher(cfg).enrich(Record(payload={}))


@respx.mock
async def test_lookup_result_lands_in_the_context():
    respx.get("http://cmdb/hosts/esxi-07").mock(
        return_value=httpx.Response(200, json={"cluster": "prod-01"})
    )
    cfg = EnrichmentConfig(
        lookups=[{"name": "cmdb", "url": "http://cmdb/hosts/{host}"}],
        prompt={"template": "{{ context.cmdb.cluster }}"},
    )
    enriched = await Enricher(cfg).enrich(Record(payload={"host": "esxi-07"}))
    assert enriched.context["cmdb"] == {"cluster": "prod-01"}
    assert enriched.user_prompt == "prod-01"


@respx.mock
async def test_optional_lookup_failure_is_tolerated():
    respx.get("http://cmdb/hosts/x").mock(return_value=httpx.Response(500))
    cfg = EnrichmentConfig(
        lookups=[{"name": "cmdb", "url": "http://cmdb/hosts/x", "optional": True}],
        prompt={"template": "{{ context.cmdb }}"},
    )
    enriched = await Enricher(cfg).enrich(Record(payload={}))
    assert enriched.context["cmdb"] is None


@respx.mock
async def test_required_lookup_failure_propagates():
    respx.get("http://cmdb/hosts/x").mock(return_value=httpx.Response(500))
    cfg = EnrichmentConfig(
        lookups=[{"name": "cmdb", "url": "http://cmdb/hosts/x", "optional": False}],
        prompt={"template": "{{ context.cmdb }}"},
    )
    with pytest.raises(RuntimeError, match="lookup 'cmdb' failed"):
        await Enricher(cfg).enrich(Record(payload={}))
