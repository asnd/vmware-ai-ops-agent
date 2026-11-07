import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alarm_delta import Snapshot, alarm_key, compute_delta, teams_card  # noqa: E402

INV = {"edge01": {"fabric": "F1", "role": "edge-leaf"}}
LINK = {"type": "linkDown", "resource": "Eth49/1", "severity": "major", "text": "Eth49/1 down"}


def run(state, alarms, reachable=True, **kw):
    snaps = {"edge01": Snapshot("edge01", reachable, alarms)}
    return [e.kind for e in compute_delta(snaps, state, INV, **kw)]


def test_new_requires_confirmation_then_clears_after_holddown():
    st: dict = {}
    assert run(st, [LINK]) == []  # first sighting: pending
    assert run(st, [LINK]) == ["NEW"]  # confirmed
    assert run(st, [LINK]) == []  # no repeat
    assert run(st, []) == []  # one missing poll: still active
    assert run(st, []) == ["CLEARED"]
    assert st["alarms"] == {}


def test_short_flap_never_notifies():
    st: dict = {}
    assert run(st, [LINK]) == []
    assert run(st, []) == []
    assert run(st, []) == []
    assert st["alarms"] == {}


def test_severity_change():
    st: dict = {}
    run(st, [LINK], confirm=1)
    assert run(st, [{**LINK, "severity": "critical"}], confirm=1) == ["CHANGED"]


def test_unreachable_device_keeps_alarms():
    st: dict = {}
    run(st, [LINK], confirm=1)
    assert run(st, [], reachable=False, clear_after=1) == ["UNREACHABLE"]
    assert len(st["alarms"]) == 1  # not cleared while we can't see the device
    assert run(st, [LINK], clear_after=1) == ["RECOVERED"]


def test_key_ignores_timestamps_when_no_resource():
    a = {"type": "envAlarm", "text": "PSU2 failed at 2026-11-07T10:00:01Z"}
    b = {"type": "envAlarm", "text": "PSU2 failed at 2026-11-07T10:05:03Z"}
    assert alarm_key("edge01", a) == alarm_key("edge01", b)


def test_teams_card_shape():
    st: dict = {}
    snaps = {"edge01": Snapshot("edge01", True, [LINK])}
    events = compute_delta(snaps, st, INV, confirm=1)
    card = teams_card(events, "t")
    content = card["attachments"][0]["content"]
    assert content["type"] == "AdaptiveCard"
    assert any(b.get("type") == "FactSet" for b in content["body"])
