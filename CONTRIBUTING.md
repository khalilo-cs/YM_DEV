# Contributing to YEMENDEV AI

Thank you for helping. This project values **verifiable correctness over speed**. These rules are
enforced in review.

## Ground rules

1. **No fake functionality.** Unfinished work is labelled (`NOT STARTED`, `EXPERIMENTAL`, `Requires a
   model`, ...) in `docs/FEATURE_STATUS.md`. Never return fabricated success.
2. **Every fixed bug gets a regression test.** Name it `test_regression_*` or reference the finding id.
3. **Never weaken or delete a failing test to get green.** If a test is wrong, explain why in the commit.
4. **Security first.** Untrusted inputs are project code, model output and API requests. Read
   `docs/THREAT_MODEL.md` before touching `sandbox/`, `governance/`, `security/` or `recovery/`.
5. **Docs match code.** Update `FEATURE_STATUS.md`, `CHANGELOG.md` and, for significant decisions, add an
   ADR in `docs/adr/`. Reference docs are generated: run `python scripts/gen_reference_docs.py`.

## Workflow

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/ruff check src tests scripts
.venv/bin/mypy                       # strict
.venv/bin/python -m pytest           # full suite; docker tests run when a daemon + image exist
```

Before opening a pull request: all three commands pass, new behaviour has tests, and you have read your
own diff for bugs, dead code and error-handling gaps.

## Where things live

`src/yemendev/<engine>/` - one package per engine (see `docs/ARCHITECTURE.md`). Only
`platform.py` wires engines together. Tests mirror the levels in `docs/TESTING_GUIDE.md`.

## License

The project license is **pending the owner's decision** (`pyproject.toml` currently declares
Apache-2.0 provisionally; there is no `LICENSE` file yet). Contributions are accepted on the
understanding that they will be released under the license the owner chooses.
