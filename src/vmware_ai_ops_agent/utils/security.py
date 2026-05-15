"""
Security utilities for data sanitization and protection.
"""

import re

# Regex for IPv4 addresses
IPV4_PATTERN = r"\b(?:\d{1,3}\.){3}\d{1,3}\b"

# Regex for IPv6 addresses (simplified - covers most common formats)
IPV6_PATTERN = r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b"

# Regex for common secret patterns (simplified)
SECRET_PATTERN = (
    r"(?i)(api[_-]?key|secret|password|token|auth|credential|private[_-]?key)"
    r'[\s:=]+[\'"]?[\w\-]+[\'"]?'
)

# Regex for email addresses
EMAIL_PATTERN = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"

# Regex for UUIDs (common in VMware resource identifiers)
UUID_PATTERN = r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"

# Regex for JWT tokens
JWT_PATTERN = r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"

# Regex for base64-encoded secrets (long base64 strings often are credentials)
BASE64_SECRET_PATTERN = r"(?i)(bearer|basic|authorization)[\s:]+[A-Za-z0-9+/=]{40,}"


def scrub_sensitive_data(text: str) -> str:
    """
    Redact sensitive information from text strings.

    Removes:
    - IPv4 and IPv6 addresses
    - Email addresses
    - UUIDs (resource identifiers)
    - JWT tokens
    - Common secret/credential patterns
    - Base64-encoded authorization headers
    """
    if not text:
        return text

    # Redact IPs (v4 and v6)
    text = re.sub(IPV4_PATTERN, "[REDACTED_IP]", text)
    text = re.sub(IPV6_PATTERN, "[REDACTED_IP]", text)

    # Redact Emails
    text = re.sub(EMAIL_PATTERN, "[REDACTED_EMAIL]", text)

    # Redact UUIDs
    text = re.sub(UUID_PATTERN, "[REDACTED_UUID]", text)

    # Redact JWT tokens
    text = re.sub(JWT_PATTERN, "[REDACTED_JWT]", text)

    # Redact base64 auth headers
    text = re.sub(BASE64_SECRET_PATTERN, r"\1: [REDACTED]", text)

    # Redact Secrets (matches "key: value" or "key=value")
    def redact_secret(match):
        key = match.group(1)
        return f"{key}: [REDACTED]"

    text = re.sub(SECRET_PATTERN, redact_secret, text)

    return text
