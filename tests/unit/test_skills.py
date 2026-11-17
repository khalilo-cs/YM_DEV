import pytest

from yemendev.core.contracts import ConflictError, InvalidInputError, PreconditionFailedError
from yemendev.learning.experience import candidate_skill, extract_token_rename
from yemendev.learning.skills import (
    Preconditions,
    SkillEngine,
    SkillSpec,
    SkillStatus,
    SkillTestCase,
    TokenRenameParams,
    apply_token_rename,
    python_version_ok,
)


def spec(expected="x = m.add(1)\n", old="m.add_numbers", new="m.add"):
    return SkillSpec(name="r", description="d", params=TokenRenameParams(old=old, new=new),
                     preconditions=Preconditions(hypothesis_category="api_mismatch",
                                                 signals={"attribute": "add_numbers"}),
                     test_cases=[SkillTestCase(name="c", input="x = m.add_numbers(1)\n", expected=expected)])


def test_token_rename_word_boundaries():
    p = TokenRenameParams(old="m.add_numbers", new="m.add")
    out, n = apply_token_rename(p, "m.add_numbers(1); xm.add_numbers(2); m.add_numbers_v2(); a.m.add_numbers()")
    assert n == 1 and out.startswith("m.add(1); xm.add_numbers(2); m.add_numbers_v2(); a.m.add_numbers()")


def test_params_reject_non_identifiers():
    with pytest.raises(ValueError):
        TokenRenameParams(old="(a+)+$", new="x")


def test_lifecycle_happy_path(db):
    e = SkillEngine(db)
    s = e.propose(spec(), "human:a")
    assert s.status is SkillStatus.PROPOSED
    with pytest.raises(PreconditionFailedError):
        e.check_activatable(s.id, 1)
    assert e.validate(s.id).passed
    assert e.get(s.id).status is SkillStatus.VERIFIED
    assert e.activate(s.id, 1).status is SkillStatus.ACTIVE


def test_failed_validation_is_rejected_and_cannot_activate(db):
    e = SkillEngine(db)
    s = e.propose(spec(expected="x = WRONG\n"), "agent:learner")
    assert not e.validate(s.id).passed
    assert e.get(s.id).status is SkillStatus.REJECTED
    with pytest.raises(PreconditionFailedError):
        e.activate(s.id, 1)


def test_tampered_spec_fails_validation(db):
    e = SkillEngine(db)
    s = e.propose(spec(), "human:a")
    with db.transaction() as conn:
        conn.execute("UPDATE skills SET spec_json = replace(spec_json, 'm.add\"', 'os.system\"')")
    assert not e.validate(s.id).passed


def test_new_version_starts_proposed_and_one_active(db):
    e = SkillEngine(db)
    s = e.propose(spec(), "human:a")
    e.validate(s.id)
    e.activate(s.id, 1)
    with pytest.raises(ConflictError):
        e.propose(spec(), "human:a", skill_id=s.id)
    v2 = e.propose(spec(expected="x = m.add2(1)\n", new="m.add2"), "human:a", skill_id=s.id)
    assert v2.version == 2 and v2.status is SkillStatus.PROPOSED
    e.validate(s.id, 2)
    e.activate(s.id, 2)
    assert e.get(s.id, 1).status is SkillStatus.DEPRECATED
    assert [x.version for x in e.list(SkillStatus.ACTIVE)] == [2]


def test_trivial_test_cases_rejected(db):
    with pytest.raises(InvalidInputError):
        SkillEngine(db).propose(spec(expected="x = m.add_numbers(1)\n"), "human:a")


def test_field_failures_deprecate(db):
    e = SkillEngine(db)
    s = e.propose(spec(), "human:a")
    e.validate(s.id)
    e.activate(s.id, 1)
    e.record_field_outcome(s.id, 1, True, "evd_1")
    e.record_field_outcome(s.id, 1, False, "evd_2")
    assert e.get(s.id).status is SkillStatus.ACTIVE
    e.record_field_outcome(s.id, 1, False, "evd_3")
    assert e.get(s.id).status is SkillStatus.DEPRECATED
    assert e.field_stats(s.id, 1) == {"successes": 1, "failures": 2, "confidence": 0.4}


def test_python_version_spec():
    assert python_version_ok(">=3.8,<4.0", (3, 13))
    assert not python_version_ok(">=3.14", (3, 13))
    assert not python_version_ok("garbage", (3, 13))


def test_extract_token_rename():
    a = {"x.py": "r = mathutils.add_numbers(t, x)\ny = mathutils.add_numbers(1, 2)\n"}
    b = {"x.py": "r = mathutils.add(t, x)\ny = mathutils.add(1, 2)\n"}
    p = extract_token_rename(a, b)
    assert p and (p.old, p.new) == ("mathutils.add_numbers", "mathutils.add")
    assert extract_token_rename(a, {"x.py": "r = mathutils.add(t, x)\ny = other(1, 2)\n"}) is None
    assert extract_token_rename(a, a) is None


def test_candidate_skill_validates(db):
    a = {"x.py": "r = mathutils.add_numbers(t, x)\n"}
    b = {"x.py": "r = mathutils.add(t, x)\n"}
    c = candidate_skill(a, b, "api_mismatch", {"attribute": "add_numbers"}, origin="evd_1")
    e = SkillEngine(db)
    s = e.propose(c, "agent:learning")
    assert e.validate(s.id).passed
