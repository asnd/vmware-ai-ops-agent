"""
VMware AI Ops Agent

AI-powered proactive maintenance agent for VMware vROps and vRLI.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .agent import VMwareAIOpsAgent
from .config import Settings, load_settings

try:
    __version__ = _pkg_version("vmware-ai-ops-agent")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

__all__ = ["VMwareAIOpsAgent", "Settings", "load_settings"]
