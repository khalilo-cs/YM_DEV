from yemendev.security.redaction import REDACTED, redact, redact_text


def test_redacts_known_token_shapes():
    samples = [
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_" + "a" * 36,
        "sk-" + "b" * 40,
        "ydv_" + "c" * 40,
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    ]
    for s in samples:
        out = redact_text(s)
        assert REDACTED in out, s
        assert "abcdefghijklmnop" not in out


def test_redacts_url_credentials_and_assignments():
    assert redact_text("postgres://user:hunter2@db:5432/x") == f"postgres://{REDACTED}@db:5432/x"
    out = redact_text('password = "hunter2xyz"')
    assert "hunter2xyz" not in out and out.startswith("password = ")


def test_redacts_sensitive_keys_recursively():
    out = redact({"api_key": "x123", "nested": [{"password": "p4ss", "ok": "fine"}], "note": "hello"})
    assert out == {"api_key": REDACTED, "nested": [{"password": REDACTED, "ok": "fine"}], "note": "hello"}


def test_plain_text_untouched():
    assert redact_text("def test_token_count(): assert 1") == "def test_token_count(): assert 1"


def test_regression_counts_with_password_like_keys_are_not_redacted():
    # Regression: 'passed' matched the old r"pass(word|wd)?" key regex and destroyed test counts.
    data = {"passed": 3, "failed": 1, "max_tokens": 100, "token_count": 7, "author": "x"}
    assert redact(data) == data


def test_sensitive_key_variants():
    from yemendev.security.redaction import is_sensitive_key
    for k in ["api_key", "apiKey", "X-Auth-Token", "PRIVATE_KEY", "db_password", "Authorization", "client_secret"]:
        assert is_sensitive_key(k), k
    for k in ["passed", "max_tokens", "tokenizer", "author", "session_count", "keys"]:
        assert not is_sensitive_key(k), k
