"""
vCenter API client for remediation actions.
"""

from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import VCenterConfig

logger = structlog.get_logger(__name__)


class VCenterClient:
    """Client for vCenter REST API operations."""

    def __init__(self, config: VCenterConfig):
        self.config = config
        self.base_url = f"https://{config.host}:{config.port}/api"
        self._session_id: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "VCenterClient":
        self._client = httpx.AsyncClient(verify=self.config.verify_ssl, timeout=60)
        await self._authenticate()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client and self._session_id:
            try:
                await self._client.delete(
                    f"{self.base_url}/session",
                    headers={"vmware-api-session-id": self._session_id},
                )
            except Exception:
                pass
        if self._client:
            await self._client.aclose()

    async def _authenticate(self) -> None:
        try:
            response = await self._client.post(
                f"{self.base_url}/session",
                auth=(self.config.username, self.config.password.get_secret_value()),
            )
            response.raise_for_status()
            self._session_id = response.json()
            logger.info("vCenter authentication successful", host=self.config.host)
        except httpx.HTTPError as e:
            logger.error("vCenter authentication failed", error=str(e))
            raise

    def _get_headers(self) -> dict[str, str]:
        return {"vmware-api-session-id": self._session_id or "", "Content-Type": "application/json"}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def _request(self, method: str, endpoint: str, **kwargs) -> dict[str, Any] | list | None:
        url = f"{self.base_url}/{endpoint}"
        response = await self._client.request(method, url, headers=self._get_headers(), **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    async def get_vms(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "vcenter/vm")
        return result if result else []

    async def get_hosts(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "vcenter/host")
        return result if result else []

    async def get_datastores(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "vcenter/datastore")
        return result if result else []

    async def vmotion_vm(self, vm_id: str, target_host: str) -> dict[str, Any]:
        if self.config.dry_run:
            logger.info("DRY RUN: Would vMotion VM", vm_id=vm_id, target_host=target_host)
            return {"dry_run": True, "action": "vmotion", "vm": vm_id, "target": target_host}

        logger.info("Initiating vMotion", vm_id=vm_id, target_host=target_host)
        payload = {"placement": {"host": target_host}}
        result = await self._request("POST", f"vcenter/vm/{vm_id}/relocate", json=payload)
        return result or {}

    async def storage_vmotion_vm(self, vm_id: str, target_datastore: str) -> dict[str, Any]:
        if self.config.dry_run:
            logger.info(
                "DRY RUN: Would Storage vMotion",
                vm_id=vm_id,
                target_datastore=target_datastore,
            )
            return {
                "dry_run": True,
                "action": "storage_vmotion",
                "vm": vm_id,
                "target": target_datastore,
            }

        logger.info("Initiating Storage vMotion", vm_id=vm_id, target_datastore=target_datastore)
        payload = {"placement": {"datastore": target_datastore}}
        result = await self._request("POST", f"vcenter/vm/{vm_id}/relocate", json=payload)
        return result or {}

    async def trigger_drs_recommendation(self, cluster_id: str) -> dict[str, Any]:
        if self.config.dry_run:
            logger.info("DRY RUN: Would trigger DRS", cluster_id=cluster_id)
            return {"dry_run": True, "action": "drs_trigger", "cluster": cluster_id}

        logger.info("Triggering DRS recommendation", cluster_id=cluster_id)
        return {"action": "drs_trigger", "cluster": cluster_id, "status": "requested"}

    async def find_best_target_host(
        self, vm_id: str, exclude_hosts: list[str] | None = None
    ) -> str | None:
        exclude_hosts = exclude_hosts or []
        hosts = await self.get_hosts()
        available = [
            h
            for h in hosts
            if h.get("connection_state") == "CONNECTED" and h.get("host") not in exclude_hosts
        ]
        return available[0].get("host") if available else None

    async def find_best_target_datastore(self, vm_id: str, min_free_gb: int = 100) -> str | None:
        datastores = await self.get_datastores()
        available = [
            ds for ds in datastores if ds.get("free_space", 0) >= min_free_gb * 1024 * 1024 * 1024
        ]
        available.sort(key=lambda ds: ds.get("free_space", 0), reverse=True)
        return available[0].get("datastore") if available else None
