"""
Security utilities for data sanitization and protection.
"""

import re

# Regex for IPv4 addresses
IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# Regex for IPv6 addresses (simplified - covers most common formats)
IPV6_PATTERN = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")

# Regex for common secret assignments.
SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|secret|password|token|auth|credential|private[_-]?key)"
    r"\b\s*[:=]\s*['\"]?[^'\"\s,}\]]+['\"]?"
)

# Regex for Authorization-style credentials. Keep the auth scheme for diagnostics.
AUTH_HEADER_PATTERN = re.compile(
    r"(?i)\b(authorization)\b\s*[:=]\s*(?:(bearer|basic)\s+)?[A-Za-z0-9._~+/=-]+"
)

# Regex for email addresses
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Regex for UUIDs (common in VMware resource identifiers)
UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# Regex for JWT tokens
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")

# Regex for base64-encoded secrets (long base64 strings often are credentials)
BASE64_SECRET_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9+/=]{40,}")


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
    text = IPV4_PATTERN.sub("[REDACTED_IP]", text)
    text = IPV6_PATTERN.sub("[REDACTED_IP]", text)

    # Redact Emails
    text = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)

    # Redact UUIDs
    text = UUID_PATTERN.sub("[REDACTED_UUID]", text)

    # Redact JWT tokens
    text = JWT_PATTERN.sub("[REDACTED_JWT]", text)

    # Redact Authorization headers and bare Bearer/Basic tokens before generic secrets.
    def redact_auth_header(match: re.Match[str]) -> str:
        scheme = match.group(2)
        if scheme:
            return f"{match.group(1)}: {scheme} [REDACTED]"
        return f"{match.group(1)}: [REDACTED]"

    text = AUTH_HEADER_PATTERN.sub(redact_auth_header, text)

    # Redact base64 auth headers
    text = BASE64_SECRET_PATTERN.sub(r"\1 [REDACTED]", text)

    # Redact Secrets (matches "key: value" or "key=value")
    def redact_secret(match: re.Match[str]) -> str:
        key = match.group(1)
        return f"{key}: [REDACTED]"

    text = SECRET_PATTERN.sub(redact_secret, text)

    return text
