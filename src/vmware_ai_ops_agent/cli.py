"""
Command-line interface for VMware AI Ops Agent.
"""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import structlog
import typer
from rich.console import Console
from rich.table import Table

from .agent import VMwareAIOpsAgent
from .config import load_settings

app = typer.Typer(
    name="vmware-ai-agent",
    help="VMware AI Ops Agent - Intelligent infrastructure management",
    add_completion=False,
)
console = Console()


def setup_logging(level: str = "INFO") -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            (
                structlog.dev.ConsoleRenderer()
                if level == "DEBUG"
                else structlog.processors.JSONRenderer()
            ),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


@app.command()
def run(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Configuration file path")
    ] = None,
    cycle_interval: Annotated[
        int | None, typer.Option("--interval", "-i", help="Override cycle interval")
    ] = None,
    log_level: Annotated[str, typer.Option("--log-level", "-l", help="Log level")] = "INFO",
    metrics_port: Annotated[
        int | None, typer.Option("--metrics-port", help="Metrics server port")
    ] = None,
) -> None:
    """Run the VMware AI Ops Agent continuously."""
    setup_logging(log_level)
    logger = structlog.get_logger(__name__)

    try:
        settings = load_settings(config)

        if cycle_interval:
            settings.agent.cycle_interval = cycle_interval
        if metrics_port:
            settings.metrics.port = metrics_port

        console.print("[bold green]Starting VMware AI Ops Agent[/bold green]")
        console.print(f"  Cycle interval: {settings.agent.cycle_interval}s")
        console.print(f"  Metrics port: {settings.metrics.port}")
        auto_rem = "enabled" if settings.agent.auto_remediate.enabled else "disabled"
        console.print(f"  Auto-remediation: {auto_rem}")

        agent = VMwareAIOpsAgent(settings)

        async def run_agent():
            try:
                await agent.start()
                while agent.state.running:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                logger.info("Shutdown requested")
            finally:
                await agent.stop()

        asyncio.run(run_agent())

    except FileNotFoundError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1) from e
    except Exception as e:
        logger.error("Agent failed", error=str(e))
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e


@app.command()
def analyze(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Configuration file path")
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Output file for results")
    ] = None,
    out_format: Annotated[
        str, typer.Option("--format", "-f", help="Output format (text, json)")
    ] = "text",
) -> None:
    """Run a single analysis cycle and exit."""
    setup_logging("INFO")

    try:
        settings = load_settings(config)
        agent = VMwareAIOpsAgent(settings)

        console.print("[bold]Running single analysis cycle...[/bold]")

        async def run_analysis():
            await agent.knowledge_base.initialize()
            return await agent.analyze_now()

        result = asyncio.run(run_analysis())

        if result:
            if out_format == "json":
                output_data = {
                    "summary": result.summary,
                    "urgency": result.urgency.value,
                    "findings": [f.model_dump() for f in getattr(result, "findings", [])],
                    "predictions": [p.model_dump() for p in result.predicted_failures],
                }
                if output:
                    output.write_text(json.dumps(output_data, indent=2, default=str))
                else:
                    console.print_json(data=output_data)
            else:
                table = Table(title="Analysis Results")
                table.add_column("Field", style="cyan")
                table.add_column("Value", style="white")

                table.add_row("Urgency", result.urgency.value)
                summary = result.summary
                if len(summary) > 100:
                    summary = summary[:100] + "..."
                table.add_row("Summary", summary)
                findings = getattr(result, "findings", [])
                table.add_row("Findings", str(len(findings)))
                table.add_row("Predictions", str(len(result.predicted_failures)))
                table.add_row(
                    "Has Remediation Plan",
                    "Yes" if result.remediation_plan else "No",
                )

                console.print(table)

                if findings:
                    console.print("\n[bold]Findings:[/bold]")
                    for i, finding in enumerate(findings, 1):
                        console.print(f"  {i}. [{finding.severity.value}] {finding.title}")

                if result.predicted_failures:
                    console.print("\n[bold]Predictions:[/bold]")
                    for pred in result.predicted_failures:
                        desc = getattr(pred, "description", pred.failure_type)
                        console.print(f"  - {desc} (probability: {pred.probability:.0%})")
        else:
            console.print("[yellow]No issues detected or analysis failed[/yellow]")

    except Exception as e:
        console.print(f"[red]Analysis failed:[/red] {e}")
        raise typer.Exit(1) from e


@app.command()
def status(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Configuration file path")
    ] = None,
) -> None:
    """Show agent status and statistics."""
    try:
        settings = load_settings(config)
        agent = VMwareAIOpsAgent(settings)

        status_data = agent.get_status()

        table = Table(title="Agent Status")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="white")

        for key, value in status_data.items():
            if isinstance(value, dict):
                table.add_row(key, str(value))
            else:
                table.add_row(key, str(value) if value is not None else "N/A")

        console.print(table)

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e


@app.command()
def capacity(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Configuration file path")
    ] = None,
    resource_kind: Annotated[
        str, typer.Option("--kind", "-k", help="Resource kind to report on")
    ] = "HostSystem",
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max resources to report")] = 20,
    out_format: Annotated[
        str, typer.Option("--format", "-f", help="Output format (text, json)")
    ] = "text",
) -> None:
    """Report capacity remaining and time-to-exhaustion for resources."""
    setup_logging("WARNING")

    try:
        settings = load_settings(config)

        if not settings.ariaops_mcp.enabled:
            console.print(
                "[red]AriaOps MCP is disabled[/red] — enable [bold]ariaops_mcp[/bold] to "
                "run capacity reports."
            )
            raise typer.Exit(1)

        from .mcp_clients.ariaops import AriaOpsMCPClient
        from .reporting import build_capacity_report

        async def run_report():
            client = AriaOpsMCPClient(
                base_url=settings.ariaops_mcp.url,
                auth_token=settings.ariaops_mcp.auth_token.get_secret_value() or None,
                timeout=settings.ariaops_mcp.timeout,
            )
            await client.connect()
            try:
                return await build_capacity_report(client, resource_kind, limit)
            finally:
                await client.disconnect()

        console.print(f"[bold]Capacity report for {resource_kind} (top {limit})...[/bold]")
        entries = asyncio.run(run_report())

        if out_format == "json":
            data = [
                {
                    "resource_id": e.resource_id,
                    "resource_name": e.resource_name,
                    "remaining_percent": e.remaining_percent,
                    "time_remaining_days": e.time_remaining_days,
                }
                for e in entries
            ]
            console.print_json(data=data)
            return

        if not entries:
            console.print("[yellow]No capacity data returned[/yellow]")
            return

        table = Table(title=f"Capacity — {resource_kind}")
        table.add_column("Resource", style="cyan")
        table.add_column("Remaining %", style="white", justify="right")
        table.add_column("Days to exhaustion", style="white", justify="right")

        for e in entries:
            remaining = f"{e.remaining_percent:.0f}" if e.remaining_percent is not None else "N/A"
            days = f"{e.time_remaining_days:.0f}" if e.time_remaining_days is not None else "N/A"
            soon = e.time_remaining_days is not None and e.time_remaining_days < 30
            table.add_row(e.resource_name, remaining, days, style="red" if soon else None)

        console.print(table)

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Capacity report failed:[/red] {e}")
        raise typer.Exit(1) from e


@app.command()
def validate(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Configuration file path")
    ] = None,
) -> None:
    """Validate configuration and connectivity."""
    setup_logging("WARNING")

    try:
        console.print("[bold]Validating configuration...[/bold]")

        settings = load_settings(config)
        console.print("  [green]✓[/green] Configuration loaded successfully")

        checks = [
            ("vROps", settings.vrops.host, settings.vrops.port),
            ("vRLI", settings.vrli.host, settings.vrli.port),
            ("vCenter", settings.vcenter.host, settings.vcenter.port),
            ("LLM Endpoint", settings.llm.endpoint, None),
        ]

        console.print("\n[bold]Configured endpoints:[/bold]")
        for name, host, port in checks:
            if port:
                console.print(f"  {name}: {host}:{port}")
            else:
                console.print(f"  {name}: {host}")

        console.print("\n[bold]Auto-remediation settings:[/bold]")
        console.print(f"  Enabled: {settings.agent.auto_remediate.enabled}")
        console.print(f"  Require approval: {settings.agent.auto_remediate.require_approval}")
        console.print(f"  Max actions/hour: {settings.agent.auto_remediate.max_actions_per_hour}")
        console.print(f"  Allowed: {', '.join(settings.agent.auto_remediate.allowed_actions)}")
        console.print(f"  Forbidden: {', '.join(settings.agent.auto_remediate.forbidden_actions)}")

        console.print("\n[bold green]Configuration is valid![/bold green]")

    except FileNotFoundError as e:
        console.print(f"[red]Configuration file not found:[/red] {e}")
        raise typer.Exit(1) from e
    except Exception as e:
        console.print(f"[red]Validation failed:[/red] {e}")
        raise typer.Exit(1) from e


@app.command()
def init(
    output: Annotated[Path, typer.Option("--output", "-o", help="Output path")] = Path(
        "./config/settings.yaml"
    ),
    force: Annotated[bool, typer.Option("--force", "-f", help="Overwrite existing file")] = False,
) -> None:
    """Generate a sample configuration file."""
    if output.exists() and not force:
        console.print(f"[yellow]File already exists:[/yellow] {output}")
        console.print("Use --force to overwrite")
        raise typer.Exit(1)

    sample_config = """# VMware AI Ops Agent Configuration

vrops:
  host: vrops.example.com
  port: 443
  username: ${VROPS_USERNAME}
  password: ${VROPS_PASSWORD}
  verify_ssl: true

vrli:
  host: vrli.example.com
  port: 443
  username: ${VRLI_USERNAME}
  password: ${VRLI_PASSWORD}
  verify_ssl: true

vcenter:
  host: vcenter.example.com
  port: 443
  username: ${VCENTER_USERNAME}
  password: ${VCENTER_PASSWORD}
  verify_ssl: true
  dry_run: true  # Set to false to enable actual remediation

llm:
  endpoint: http://localhost:8000/v1
  api_key: ${LLM_API_KEY}
  model: mistral-7b-instruct
  max_tokens: 4096
  temperature: 0.1

vector_db:
  type: faiss
  persist_directory: ./data/faiss
  collection_name: vmware_incidents

agent:
  cycle_interval: 300  # seconds
  thresholds:
    cpu_critical: 90
    cpu_warning: 80
    memory_critical: 95
    memory_warning: 85
    disk_latency_critical: 50
  auto_remediate:
    enabled: false
    require_approval: true
    max_actions_per_hour: 10
    allowed_actions:
      - vmotion
      - drs_rebalance
      - snapshot_cleanup
    forbidden_actions:
      - vm_power_off
      - host_maintenance_mode

notifications:
  slack:
    enabled: false
    webhook_url: ${SLACK_WEBHOOK_URL}
    channel: "#vmware-ops"
  email:
    enabled: false
    smtp_host: smtp.example.com
    smtp_port: 587

logging:
  level: INFO
  format: json
  file: ./logs/agent.log

metrics:
  enabled: true
  port: 9090
"""

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(sample_config)
    console.print(f"[green]Sample configuration written to:[/green] {output}")
    console.print("\nNext steps:")
    console.print("  1. Edit the configuration file with your environment details")
    console.print("  2. Set environment variables for sensitive values")
    console.print("  3. Run: vmware-ai-agent validate -c " + str(output))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
