#!/usr/bin/env python3
"""Stateful alarm delta engine for DC switch fabrics (prototype, stdlib only).

Run it right after the poller has written one snapshot file per device. It
compares the current snapshot set with the previous state, works out which
alarms are NEW, CLEARED or CHANGED (severity), applies confirm/clear
hold-downs against flapping, and sends grouped notifications to Microsoft Teams
(Workflows webhook, Adaptive Card) and/or e-mail.

Snapshot format (one file per device, ``<snapshot-dir>/**/<device>.json``)::

    {
      "device": "dc1-f07-edge01",
      "reachable": true,
      "alarms": [
        {"type": "linkDown", "resource": "Ethernet49/1",
         "severity": "major", "text": "Interface Ethernet49/1 is down"}
      ]
    }

If your current cron script writes a different format, adapt ``load_snapshots``
(only that function needs to know about it).

Inventory (optional, JSON or YAML if PyYAML is installed)::

    {"dc1-f07-edge01": {"fabric": "DC1-F07", "role": "edge-leaf"}}

Environment variables for notifications (secrets are never taken from argv):
    TEAMS_WEBHOOK_URL      Teams Workflows "webhook request received" URL
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_STARTTLS (1/0)
    MAIL_FROM, MAIL_TO     (MAIL_TO is comma separated)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
import tempfile
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "warning": 3, "info": 4}

# Volatile fragments stripped before hashing so that a re-polled alarm keeps
# the same key: ISO/syslog timestamps, uptimes, bare counters.
_VOLATILE = [
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?"),
    re.compile(r"\b[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\b"),
    re.compile(r"\b\d+d\d+h\d+m(\d+s)?\b"),
    re.compile(r"\b\d{5,}\b"),
]


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalize_text(text: str) -> str:
    for rx in _VOLATILE:
        text = rx.sub("#", text)
    return " ".join(text.split()).lower()


def alarm_key(device: str, alarm: dict[str, Any]) -> str:
    """Stable identity of an alarm across polls."""
    parts = [
        device,
        str(alarm.get("type", "")),
        str(alarm.get("resource", "")),
        # Text only disambiguates alarms that have no explicit resource.
        "" if alarm.get("resource") else normalize_text(str(alarm.get("text", ""))),
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


@dataclass
class Event:
    kind: str  # NEW | CLEARED | CHANGED | UNREACHABLE | RECOVERED
    device: str
    fabric: str
    role: str
    severity: str = "info"
    type: str = ""
    resource: str = ""
    text: str = ""
    detail: str = ""


@dataclass
class Snapshot:
    device: str
    reachable: bool
    alarms: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- io


def load_snapshots(snapshot_dir: Path) -> dict[str, Snapshot]:
    snaps: dict[str, Snapshot] = {}
    for path in sorted(snapshot_dir.rglob("*.json")):
        data = json.loads(path.read_text())
        device = data.get("device") or path.stem
        snaps[device] = Snapshot(
            device=device,
            reachable=bool(data.get("reachable", True)),
            alarms=list(data.get("alarms", [])),
        )
    return snaps


def load_inventory(path: Path | None) -> dict[str, dict[str, str]]:
    if not path:
        return {}
    raw = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml  # type: ignore[import-untyped]  # optional dependency

        return yaml.safe_load(raw) or {}
    return json.loads(raw)


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text())
    return {"alarms": {}, "devices": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic write so a crash never leaves a truncated state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


# ----------------------------------------------------------------------- engine


def compute_delta(
    snaps: dict[str, Snapshot],
    state: dict[str, Any],
    inventory: dict[str, dict[str, str]],
    confirm: int = 2,
    clear_after: int = 2,
    ts: str | None = None,
) -> list[Event]:
    """Update ``state`` in place and return the events to notify."""
    ts = ts or now_iso()
    alarms: dict[str, dict[str, Any]] = state.setdefault("alarms", {})
    devices: dict[str, dict[str, Any]] = state.setdefault("devices", {})
    events: list[Event] = []

    def meta(dev: str) -> tuple[str, str]:
        inv = inventory.get(dev, {})
        return inv.get("fabric", "unknown"), inv.get("role", "unknown")

    # Device reachability. Unreachable devices keep their alarms untouched:
    # "no answer" must never be read as "all alarms cleared".
    for dev, snap in snaps.items():
        fabric, role = meta(dev)
        was_up = devices.get(dev, {}).get("reachable", True)
        devices[dev] = {"reachable": snap.reachable, "last_poll": ts}
        if was_up and not snap.reachable:
            events.append(
                Event("UNREACHABLE", dev, fabric, role, "major", detail="SNMP poll failed")
            )
        elif not was_up and snap.reachable:
            events.append(
                Event("RECOVERED", dev, fabric, role, "info", detail="SNMP poll OK again")
            )

    polled = {d for d, s in snaps.items() if s.reachable}
    seen_now: set[str] = set()

    for dev in sorted(polled):
        fabric, role = meta(dev)
        for alarm in snaps[dev].alarms:
            key = alarm_key(dev, alarm)
            if key in seen_now:  # duplicate rows in one snapshot
                continue
            seen_now.add(key)
            sev = str(alarm.get("severity", "info")).lower()
            rec = alarms.get(key)
            if rec is None:
                rec = alarms[key] = {
                    "device": dev,
                    "type": alarm.get("type", ""),
                    "resource": alarm.get("resource", ""),
                    "text": alarm.get("text", ""),
                    "severity": sev,
                    "first_seen": ts,
                    "seen": 0,
                    "missing": 0,
                    "active": False,
                }
            rec["seen"] += 1
            rec["missing"] = 0
            rec["last_seen"] = ts
            rec["text"] = alarm.get("text", rec["text"])
            ev = Event("", dev, fabric, role, sev, rec["type"], rec["resource"], rec["text"])
            if not rec["active"] and rec["seen"] >= confirm:
                rec["active"] = True
                rec["severity"] = sev
                ev.kind = "NEW"
                events.append(ev)
            elif rec["active"] and sev != rec["severity"]:
                ev.kind = "CHANGED"
                ev.detail = f"severity {rec['severity']} -> {sev}"
                rec["severity"] = sev
                events.append(ev)
            elif not rec["active"]:
                rec["severity"] = sev

    # Alarms that disappeared from a device that *did* answer this time.
    for key in list(alarms):
        rec = alarms[key]
        if rec["device"] not in polled or key in seen_now:
            continue
        rec["missing"] += 1
        rec["seen"] = 0
        if rec["missing"] >= clear_after:
            if rec["active"]:
                fabric, role = meta(rec["device"])
                events.append(
                    Event(
                        "CLEARED",
                        rec["device"],
                        fabric,
                        role,
                        rec["severity"],
                        rec["type"],
                        rec["resource"],
                        rec["text"],
                        detail=f"active since {rec['first_seen']}",
                    )
                )
            del alarms[key]  # unconfirmed flaps vanish silently

    state["last_run"] = ts
    return events


# ----------------------------------------------------------------- notification


def _sort_key(e: Event) -> tuple[Any, ...]:
    return (e.fabric, SEVERITY_ORDER.get(e.severity, 9), e.device, e.resource)


def summarize(events: list[Event]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for e in events:
        counts[e.kind] += 1
    if not counts:
        return "no alarm changes"
    order = ["NEW", "CHANGED", "CLEARED", "UNREACHABLE", "RECOVERED"]
    return ", ".join(f"{counts[k]} {k}" for k in order if counts[k])


def render_text(events: list[Event]) -> str:
    lines: list[str] = []
    by_fabric: dict[str, list[Event]] = defaultdict(list)
    for e in sorted(events, key=_sort_key):
        by_fabric[e.fabric].append(e)
    for fabric, evs in by_fabric.items():
        lines.append(f"== Fabric {fabric} ==")
        for e in evs:
            what = " ".join(x for x in (e.type, e.resource) if x)
            tail = e.detail or e.text
            lines.append(f"  [{e.kind:<11}] {e.severity:<8} {e.device} ({e.role}) {what} - {tail}")
    return "\n".join(lines)


def teams_card(events: list[Event], title: str) -> dict[str, Any]:
    """Adaptive Card payload as expected by a Teams Workflows webhook."""
    icon = {"NEW": "🔴", "CHANGED": "🟠", "CLEARED": "🟢", "UNREACHABLE": "⚫", "RECOVERED": "🔵"}
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "size": "Medium", "weight": "Bolder", "text": title, "wrap": True}
    ]
    by_fabric: dict[str, list[Event]] = defaultdict(list)
    for e in sorted(events, key=_sort_key):
        by_fabric[e.fabric].append(e)
    for fabric, evs in by_fabric.items():
        body.append(
            {
                "type": "TextBlock",
                "weight": "Bolder",
                "text": f"Fabric {fabric}",
                "separator": True,
                "wrap": True,
            }
        )
        facts = [
            {
                "title": f"{icon.get(e.kind, '')} {e.kind} {e.device}",
                "value": f"{e.severity} · {' '.join(x for x in (e.type, e.resource) if x)} · "
                f"{e.detail or e.text}",
            }
            for e in evs[:40]  # keep the card under Teams' payload limit
        ]
        if len(evs) > 40:
            facts.append({"title": "…", "value": f"{len(evs) - 40} more events"})
        body.append({"type": "FactSet", "facts": facts})
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "msteams": {"width": "Full"},
                    "body": body,
                },
            }
        ],
    }


def send_teams(url: str, payload: dict[str, Any]) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - URL from operator env
        resp.read()


def send_mail(subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["MAIL_FROM"]
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content(text)
    with smtplib.SMTP(
        os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "25")), timeout=15
    ) as s:
        if os.environ.get("SMTP_STARTTLS", "0") == "1":
            s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)


def notify(events: list[Event], min_severity: str, dry_run: bool) -> None:
    limit = SEVERITY_ORDER.get(min_severity, 4)
    # Reachability changes always go out; alarms are filtered by severity.
    wanted = [
        e
        for e in events
        if e.kind in ("UNREACHABLE", "RECOVERED") or SEVERITY_ORDER.get(e.severity, 9) <= limit
    ]
    if not wanted:
        return
    title = f"DC fabric alarms: {summarize(wanted)}"
    text = render_text(wanted)
    if dry_run:
        print("--- dry-run: would notify ---")
        print(json.dumps(teams_card(wanted, title), ensure_ascii=False, indent=2)[:2000])
        return
    errors: list[str] = []
    if url := os.environ.get("TEAMS_WEBHOOK_URL"):
        try:
            send_teams(url, teams_card(wanted, title))
        except Exception as exc:  # keep going so e-mail still goes out
            errors.append(f"teams: {exc}")
    if os.environ.get("SMTP_HOST") and os.environ.get("MAIL_TO"):
        try:
            send_mail(title, text)
        except Exception as exc:
            errors.append(f"mail: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))


# -------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshot-dir", type=Path, required=True)
    ap.add_argument("--state", type=Path, default=Path("state/alarm_state.json"))
    ap.add_argument("--inventory", type=Path)
    ap.add_argument("--confirm", type=int, default=2, help="polls an alarm must persist before NEW")
    ap.add_argument(
        "--clear-after", type=int, default=2, help="polls an alarm must be absent before CLEARED"
    )
    ap.add_argument("--min-severity", default="minor", choices=list(SEVERITY_ORDER))
    ap.add_argument(
        "--summary-file",
        type=Path,
        help="write a one-line summary (e.g. for the git commit message)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print instead of sending, do not save state"
    )
    args = ap.parse_args(argv)

    state = load_state(args.state)
    events = compute_delta(
        load_snapshots(args.snapshot_dir),
        state,
        load_inventory(args.inventory),
        confirm=args.confirm,
        clear_after=args.clear_after,
    )
    summary = summarize(events)
    print(summary)
    if events:
        print(render_text(events))
    if args.summary_file:
        args.summary_file.write_text(summary + "\n")

    try:
        notify(events, args.min_severity, args.dry_run)
    except RuntimeError as exc:
        print(f"notification failed: {exc}", file=sys.stderr)
        # State is NOT saved, so the same events are retried next run.
        return 2
    if not args.dry_run:
        save_state(args.state, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
