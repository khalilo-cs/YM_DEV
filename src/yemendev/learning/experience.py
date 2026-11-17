"""Experience extraction: derive *candidate* skills from verified repairs.

Only a repair whose verification passed is eligible. Extraction succeeds only when every
change in every patch is the same single dotted-identifier replacement (e.g.
``mathutils.add_numbers`` -> ``mathutils.add``); anything else yields no candidate.
The candidate is PROPOSED, never active: it still has to pass validation and human
activation.
"""

from __future__ import annotations

import difflib
import re

from yemendev.learning.skills import Preconditions, SkillSpec, SkillTestCase, TokenRenameParams

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*|\s+|.")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def extract_token_rename(originals: dict[str, str], patched: dict[str, str]) -> TokenRenameParams | None:
    pair: tuple[str, str] | None = None
    for path, old_text in originals.items():
        new_text = patched.get(path)
        if new_text is None:
            return None
        a, b = _tokens(old_text), _tokens(new_text)
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
            if op == "equal":
                continue
            if op != "replace" or i2 - i1 != 1 or j2 - j1 != 1:
                return None
            this = (a[i1], b[j1])
            if pair is None:
                pair = this
            elif pair != this:
                return None
    if pair is None:
        return None
    try:
        return TokenRenameParams(old=pair[0], new=pair[1])
    except ValueError:
        return None


def candidate_skill(
    originals: dict[str, str], patched: dict[str, str], hypothesis_category: str, signals: dict[str, str],
    origin: str,
) -> SkillSpec | None:
    params = extract_token_rename(originals, patched)
    if params is None:
        return None
    cases = [SkillTestCase(name=f"from:{path}", input=originals[path], expected=patched[path])
             for path in sorted(originals)]
    cases.append(SkillTestCase(name="no-op on unrelated code", input="value = other.thing()\n",
                               expected="value = other.thing()\n"))
    return SkillSpec(
        name=f"rename {params.old} -> {params.new}",
        description=f"Replace references to '{params.old}' with '{params.new}' when diagnostics report "
                    f"{hypothesis_category} with signals {signals}.",
        params=params, preconditions=Preconditions(hypothesis_category=hypothesis_category, signals=signals),
        expected_effects=[f"'{params.old}' no longer referenced in non-test source files"],
        failure_modes=["identifier also used with a different meaning elsewhere",
                       "string literals or comments containing the identifier are also rewritten"],
        test_cases=cases, origin=origin,
    )
