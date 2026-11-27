"""Stored benchmark reports for the research centre: strict names, no reads outside the report directories."""

import json
import os

import pytest

from yemendev.benchmarks import results as R
from yemendev.benchmarks.compare import CompareReport
from yemendev.core.config import Settings
from yemendev.core.contracts import InvalidInputError, NotFoundError


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_PUBLISHED", tmp_path / "published")
    return Settings(data_dir=tmp_path / "data")


def report(**kw):
    base = dict(started_at="2026-11-27T10:00:00Z", finished_at="2026-11-27T10:01:00Z", seed=7, git_commit=None,
                environment={"sandbox_backend": "process"}, arms={"A": "x"}, not_run={"B": "no model"},
                caveats=["c"], results=[], summary={"A": {"correct": "0/0"}})
    base.update(kw)
    return CompareReport(**base)


def test_save_list_and_load_round_trip(settings):
    path = R.save_report(report(), settings)
    assert path.parent == settings.data_dir / "benchmarks" and path.name.startswith("compare-")
    [row] = R.list_reports(settings)
    assert row["source"] == "local" and row["kind"] == "compare" and row["readable"]
    assert row["not_run"] == {"B": "no model"} and row["sandbox_backend"] == "process"
    got = R.load_report(settings, "local", path.name)
    assert got["report"] == json.loads(path.read_text())  # returned exactly as stored


def test_published_source_is_listed_when_present(settings, tmp_path):
    (tmp_path / "published").mkdir()
    R.save_report(report(started_at="2026-11-01T00:00:00Z"), settings, tmp_path / "published" / "old.json")
    R.save_report(report(), settings)
    rows = R.list_reports(settings)
    assert [r["source"] for r in rows] == ["local", "published"]  # newest first


def test_unreadable_files_are_listed_not_hidden_or_served(settings):
    d = settings.data_dir / "benchmarks"
    d.mkdir(parents=True)
    (d / "broken.json").write_text("{not json")
    (d / "other.json").write_text(json.dumps({"no": "results"}))
    rows = {r["name"]: r for r in R.list_reports(settings)}
    assert rows["broken.json"]["readable"] is False and rows["other.json"]["readable"] is False
    with pytest.raises(InvalidInputError):
        R.load_report(settings, "local", "broken.json")


@pytest.mark.parametrize("name", ["../secret.json", "a/b.json", ".hidden.json", "x.txt", "", "%2e%2e.json",
                                  "a" * 200 + ".json"])
def test_hostile_names_are_rejected(settings, name):
    with pytest.raises((InvalidInputError, NotFoundError)):
        R.load_report(settings, "local", name)


def test_symlinks_and_unknown_sources_are_refused(settings, tmp_path):
    d = settings.data_dir / "benchmarks"
    d.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    R.save_report(report(), settings, outside)
    os.symlink(outside, d / "link.json")
    assert R.list_reports(settings) == []
    with pytest.raises(NotFoundError):
        R.load_report(settings, "local", "link.json")
    with pytest.raises(NotFoundError):
        R.load_report(settings, "elsewhere", "x.json")


def test_only_json_report_names_are_served_even_if_the_file_is_a_valid_report(settings):
    d = settings.data_dir / "benchmarks"
    R.save_report(report(), settings, d / "notes.txt")
    with pytest.raises(InvalidInputError):
        R.load_report(settings, "local", "notes.txt")
    assert R.list_reports(settings) == []
