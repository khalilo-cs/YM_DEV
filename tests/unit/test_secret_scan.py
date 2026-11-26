"""Secret scanning of file contents (stage 18b). Samples are assembled at runtime so this file itself
contains no credential-shaped literals."""

import pytest
from helpers import make_passing_project, write

from yemendev.security.secret_scan import ALLOW_MARKER, MAX_SCAN_BYTES, scan_text
from yemendev.twin.scanner import ProjectScanner

AWS = "AKIA" + "QX7RT2LM9PZ4WK3B"
GH = "ghp" + "_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
SAMPLES = {
    "private-key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "aws-access-key-id": f'aws_key = "{AWS}"',
    "github-token": f"token: {GH}",
    "slack-token": "SLACK=" + "xox" + "b-123456789012-abcdefghijkl",
    "stripe-live-key": "stripe = '" + "sk" + "_live_" + "4eC39HqLyjWDarjtT1zdp7dc" + "'",
    "google-api-key": "key=" + "AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY",
    "ai-api-key": "OPENAI=" + "sk-" + "proj-" + "Zx8Lq2Vn7Rt4Ws9Kd3Hm6Yp1Bc5Ga0Ef",
    "jwt": "Authorization: " + "eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    "url-credentials": "DATABASE_URL=postgres://admin:" + "S3cr3tP4ss9" + "@db.internal:5432/app",
    "secret-assignment": 'client_secret = "' + "x7Gq9vL2pR4tZ8wB" + '"',
}


@pytest.mark.parametrize("rule", sorted(SAMPLES))
def test_each_rule_detects_a_realistic_credential(rule):
    hits, suppressed = scan_text(("# config\n" + SAMPLES[rule] + "\n").encode())
    assert [(h.rule, h.line) for h in hits] == [(rule, 2)] and suppressed == 0


@pytest.mark.parametrize("line", [
    'password = "changeme"', 'api_key = "${API_KEY}"', 'password = "aaaaaaaaaa"', 'secret = "<your secret>"',
    'token = "example-token-value"', "url = https://user@example.com/path", 'password = os.environ["DB_PASSWORD"]',
    "AKIA_PREFIX_IS_NOT_A_KEY", "integrity sha512-q8vV3b2Yh1n5Xw7Zm9Kp2Lr4Ts6Uv8Wx0Yz1A3B5C7D9E1F3G5H7J9K1L3M5N7P9",
])
def test_placeholders_and_benign_lines_are_not_reported(line):
    assert scan_text(line.encode()) == ([], 0)


def test_allow_marker_suppresses_and_counts():
    hits, suppressed = scan_text(f'KEY = "{AWS}"  # {ALLOW_MARKER} (test fixture)\nother = 1\n'.encode())
    assert hits == [] and suppressed == 1


def test_binary_and_oversized_content_is_not_scanned():
    assert scan_text(b"\x00\x01" + AWS.encode()) == ([], 0)
    assert scan_text(AWS.encode() + b" " * MAX_SCAN_BYTES) == ([], 0)


def test_project_scan_reports_location_never_the_value(tmp_path, platform):
    root = make_passing_project(tmp_path / "p")
    write(root, "pkg/config.py", f"DEBUG = True\nAWS_KEY = '{AWS}'\n")
    write(root, "web/app.js", f"const t = '{GH}';\n")
    write(root, "package-lock.json", '{"lockfileVersion": 3, "note": "' + AWS + '"}')  # lockfiles are skipped
    snap = ProjectScanner().scan(root)
    found = sorted((f.path, f.message.split(":")[0]) for f in snap.findings if f.kind == "secret_in_source")
    assert found == [("pkg/config.py", "line 2"), ("web/app.js", "line 1")]
    text = snap.model_dump_json()
    for secret in (AWS, GH):
        assert secret not in text and secret[4:12] not in text  # not even a fragment is stored


def test_immune_system_reports_secrets_in_source_as_security(tmp_path, platform):
    from end_to_end.conftest import ALICE

    root = make_passing_project(tmp_path / "p")
    write(root, "pkg/config.py", f"AWS_KEY = '{AWS}'\n")
    pid = platform.register_project(ALICE, "p", str(root)).id
    platform.scan_project(ALICE, pid)
    report = platform.immune_check(ALICE, pid)
    hit = [f for f in report.findings if f.rule == "secret-in-source"]
    assert len(hit) == 1 and hit[0].category == "security" and hit[0].subject == "pkg/config.py"
    assert AWS not in report.model_dump_json()
