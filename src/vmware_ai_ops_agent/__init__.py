"""
VMware AI Ops Agent

AI-powered proactive maintenance agent for VMware vROps and vRLI.
"""

from .agent import VMwareAIOpsAgent
from .config import Settings, load_settings

__version__ = "1.0.0"
__author__ = "Security Research Team"
__all__ = ["VMwareAIOpsAgent", "Settings", "load_settings"]
