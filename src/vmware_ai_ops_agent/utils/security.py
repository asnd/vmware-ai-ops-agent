"""
Security utilities for data sanitization and protection.
"""

import re

# Regex for IPv4 addresses
IPV4_PATTERN = r'\b(?:\d{1,3}\.){3}\d{1,3}\b'

# Regex for common secret patterns (simplified)
SECRET_PATTERN = r'(?i)(api[_-]?key|secret|password|token|auth)[\s:=]+[\'"]?[\w\-]+[\'"]?'

# Regex for email addresses
EMAIL_PATTERN = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'


def scrub_sensitive_data(text: str) -> str:
    """
    Redact sensitive information from text strings.
    
    Removes:
    - IPv4 addresses
    - Email addresses
    - Common secret/credential patterns
    """
    if not text:
        return text

    # Redact IPs
    text = re.sub(IPV4_PATTERN, '[REDACTED_IP]', text)
    
    # Redact Emails
    text = re.sub(EMAIL_PATTERN, '[REDACTED_EMAIL]', text)
    
    # Redact Secrets (matches "key: value" or "key=value")
    # We replace with just the key name and [REDACTED]
    def redact_secret(match):
        key = match.group(1)
        return f"{key}: [REDACTED]"
        
    text = re.sub(SECRET_PATTERN, redact_secret, text)
    
    return text
