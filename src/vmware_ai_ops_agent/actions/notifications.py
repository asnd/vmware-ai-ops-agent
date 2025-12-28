"""
Notification service for alerts and updates.
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
import structlog

from ..analysis.models import AnalysisResult, Urgency
from ..config import NotificationsConfig
from ..correlation.engine import CorrelatedIssue

logger = structlog.get_logger(__name__)


class NotificationService:
    """Service for sending notifications via multiple channels."""

    def __init__(self, config: NotificationsConfig):
        self.config = config
        self._http_client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "NotificationService":
        self._http_client = httpx.AsyncClient(timeout=30)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._http_client:
            await self._http_client.aclose()

    async def notify_issue(self, issue: CorrelatedIssue) -> dict[str, bool]:
        results = {}

        if self.config.slack.enabled:
            success = await self._send_slack(
                f"*{issue.severity.value} Issue*: {issue.description}\n"
                f"Confidence: {issue.confidence:.0%}\n"
                f"Recommended: {', '.join(issue.recommended_actions[:3])}"
            )
            results["slack"] = success

        if self.config.email.enabled:
            success = await self._send_email(
                f"[{issue.severity.value}] {issue.description[:50]}",
                f"Issue: {issue.description}\n\nRecommendations: {issue.recommended_actions}",
            )
            results["email"] = success

        return results

    async def notify_analysis(self, analysis: AnalysisResult) -> dict[str, bool]:
        results = {}

        if analysis.urgency not in (Urgency.CRITICAL, Urgency.HIGH):
            return results

        message = f"*{analysis.urgency.value.upper()} Alert*\n{analysis.summary}"

        if self.config.slack.enabled:
            results["slack"] = await self._send_slack(message)

        return results

    async def _send_slack(self, message: str) -> bool:
        try:
            webhook_url = self.config.slack.webhook_url.get_secret_value()
            if not webhook_url:
                return False

            payload = {"channel": self.config.slack.channel, "text": message, "mrkdwn": True}
            response = await self._http_client.post(webhook_url, json=payload)
            response.raise_for_status()
            logger.info("Slack notification sent")
            return True
        except Exception as e:
            logger.error("Slack notification failed", error=str(e))
            return False

    async def _send_email(self, subject: str, body: str) -> bool:
        try:
            if not self.config.email.recipients:
                return False

            msg = MIMEMultipart()
            msg["Subject"] = subject
            msg["From"] = self.config.email.from_address
            msg["To"] = ", ".join(self.config.email.recipients)
            msg.attach(MIMEText(body, "plain"))

            import asyncio

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._send_smtp, msg)
            logger.info("Email notification sent")
            return True
        except Exception as e:
            logger.error("Email notification failed", error=str(e))
            return False

    def _send_smtp(self, msg: MIMEMultipart) -> None:
        with smtplib.SMTP(self.config.email.smtp_host, self.config.email.smtp_port) as server:
            server.starttls()
            if self.config.email.smtp_user:
                password = self.config.email.smtp_password.get_secret_value()
                server.login(self.config.email.smtp_user, password)
            server.send_message(msg)
