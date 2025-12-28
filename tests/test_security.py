from vmware_ai_ops_agent.utils.security import scrub_sensitive_data


def test_scrub_sensitive_data_ips():
    text = "Connection from 192.168.1.1 to 10.0.0.5 failed."
    scrubbed = scrub_sensitive_data(text)
    assert "192.168.1.1" not in scrubbed
    assert "10.0.0.5" not in scrubbed
    assert "[REDACTED_IP]" in scrubbed


def test_scrub_sensitive_data_emails():
    text = "Contact support@vmware.com for help."
    scrubbed = scrub_sensitive_data(text)
    assert "support@vmware.com" not in scrubbed
    assert "[REDACTED_EMAIL]" in scrubbed


def test_scrub_sensitive_data_secrets():
    text = "api_key = 'abcdef123456'"
    scrubbed = scrub_sensitive_data(text)
    assert "abcdef123456" not in scrubbed
    assert "api_key: [REDACTED]" in scrubbed


def test_scrub_sensitive_data_mixed():
    text = "User admin (admin@example.com) logged in from 172.16.0.1 using password: supersecret"
    scrubbed = scrub_sensitive_data(text)
    assert "admin@example.com" not in scrubbed
    assert "172.16.0.1" not in scrubbed
    assert "supersecret" not in scrubbed
    assert "[REDACTED_EMAIL]" in scrubbed
    assert "[REDACTED_IP]" in scrubbed
