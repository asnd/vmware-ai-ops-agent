"""
Tests for the `vmware-ai-agent capacity` CLI command.
"""

from unittest.mock import AsyncMock, MagicMock

from typer.testing import CliRunner

from vmware_ai_ops_agent.cli import app
from vmware_ai_ops_agent.config import AriaOpsMCPConfig, Settings
from vmware_ai_ops_agent.reporting.capacity import CapacityEntry

runner = CliRunner()


def _settings(enabled: bool) -> Settings:
    return Settings(ariaops_mcp=AriaOpsMCPConfig(enabled=enabled))


def test_capacity_cli_disabled_exits_with_error(monkeypatch):
    monkeypatch.setattr("vmware_ai_ops_agent.cli.load_settings", lambda c: _settings(False))

    result = runner.invoke(app, ["capacity"])

    assert result.exit_code == 1
    assert "disabled" in result.output.lower()


def test_capacity_cli_json_output(monkeypatch):
    monkeypatch.setattr("vmware_ai_ops_agent.cli.load_settings", lambda c: _settings(True))

    fake_client = AsyncMock()
    fake_client.connect = AsyncMock()
    fake_client.disconnect = AsyncMock()
    monkeypatch.setattr(
        "vmware_ai_ops_agent.mcp_clients.ariaops.AriaOpsMCPClient",
        MagicMock(return_value=fake_client),
    )

    entries = [CapacityEntry("host-1", "esxi-01", 10.0, 5.0)]
    monkeypatch.setattr(
        "vmware_ai_ops_agent.reporting.build_capacity_report",
        AsyncMock(return_value=entries),
    )

    result = runner.invoke(app, ["capacity", "--format", "json", "--kind", "HostSystem"])

    assert result.exit_code == 0
    assert "esxi-01" in result.output
    fake_client.connect.assert_awaited_once()
    fake_client.disconnect.assert_awaited_once()


def test_capacity_cli_table_output(monkeypatch):
    monkeypatch.setattr("vmware_ai_ops_agent.cli.load_settings", lambda c: _settings(True))

    fake_client = AsyncMock()
    monkeypatch.setattr(
        "vmware_ai_ops_agent.mcp_clients.ariaops.AriaOpsMCPClient",
        MagicMock(return_value=fake_client),
    )
    monkeypatch.setattr(
        "vmware_ai_ops_agent.reporting.build_capacity_report",
        AsyncMock(return_value=[CapacityEntry("ds-1", "datastore-01", 80.0, 200.0)]),
    )

    result = runner.invoke(app, ["capacity", "--kind", "Datastore"])

    assert result.exit_code == 0
    assert "datastore-01" in result.output


def test_capacity_cli_empty_report(monkeypatch):
    monkeypatch.setattr("vmware_ai_ops_agent.cli.load_settings", lambda c: _settings(True))
    monkeypatch.setattr(
        "vmware_ai_ops_agent.mcp_clients.ariaops.AriaOpsMCPClient",
        MagicMock(return_value=AsyncMock()),
    )
    monkeypatch.setattr(
        "vmware_ai_ops_agent.reporting.build_capacity_report",
        AsyncMock(return_value=[]),
    )

    result = runner.invoke(app, ["capacity"])

    assert result.exit_code == 0
    assert "No capacity data" in result.output
