from yemendev.immune.rules import (
    classify_changed_file,
    classify_new_file,
    denial_bursts,
    levenshtein,
    typosquat_candidates,
)


def test_levenshtein():
    assert levenshtein("requests", "reqeusts") == 2 and levenshtein("six", "six") == 0


def test_typosquatting_flags_near_names_only():
    names = {f.subject for f in typosquat_candidates(["reqeusts", "requests", "numpi", "my-internal-lib", "six"])}
    assert names == {"reqeusts", "numpi"}
    [f] = typosquat_candidates(["djangoo"])
    assert f.category == "security" and f.certainty == "inferred" and "django" in f.detail


def test_change_classification_separates_operational_from_security():
    assert classify_changed_file("pkg/core.py", authorized=False).category == "operational"
    assert classify_changed_file("pkg/core.py", authorized=True).rule == "authorized-change"
    assert classify_changed_file("conftest.py", authorized=False).severity == "high"
    assert classify_changed_file("tests/conftest.py", authorized=False).category == "security"
    assert classify_changed_file(".github/workflows/ci.yml", authorized=False).category == "security"
    assert classify_new_file("evil.pth").severity == "high"
    assert classify_new_file("lib/native.so").category == "security"
    assert classify_new_file("docs/notes.md").category == "operational"


def test_denials_become_security_only_when_repeated():
    few = denial_bursts([("human:bob", "p1")] * 2)
    assert [f.category for f in few] == ["operational"]
    many = denial_bursts([("human:eve", f"p{i}") for i in range(6)])
    assert many[0].category == "security" and "6 denied actions across 6 project(s)" in many[0].detail
