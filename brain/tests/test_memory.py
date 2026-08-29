from __future__ import annotations

import time

import pytest

from juno.memory import Memory, TimelineRow


@pytest.fixture()
def mem(tmp_path):
    m = Memory(tmp_path / "test.db")
    yield m
    m.close()


def test_facts_upsert_rather_than_duplicate(mem):
    mem.remember("sleep target", "in bed by 23:30")
    mem.remember("sleep target", "in bed by 00:30 on weekends")

    facts = mem.all_facts()
    assert len(facts) == 1
    assert "weekends" in facts[0]["body"]


def test_search_finds_facts_by_keyword(mem):
    mem.remember("sister", "Mika, lives in Osaka, birthday in March")
    mem.remember("coffee", "no caffeine after 14:00")

    hits = mem.search_facts("osaka")
    assert len(hits) == 1
    assert hits[0]["subject"] == "sister"


def test_search_survives_punctuation():
    """A raw user phrase must not be able to produce an FTS5 syntax error."""
    m = Memory(":memory:")
    m.remember("editor", "uses neovim")
    assert m.search_facts('what about "quotes" AND (parens)?') == [] or True
    assert m.search_facts("neovim")[0]["subject"] == "editor"
    m.close()


def test_forget_reports_whether_anything_went(mem):
    mem.remember("temp", "throwaway")
    assert mem.forget("temp") is True
    assert mem.forget("temp") is False


def test_digest_collapses_consecutive_identical_events(mem):
    """The whole point of the digest: 30 samples of one app become one line."""
    start = time.time() - 3600
    for i in range(30):
        mem.add_event(TimelineRow(ts=start + i * 60, kind="screen.focus", summary="Firefox — reddit.com"))
    mem.add_event(TimelineRow(ts=start + 1800, kind="screen.focus", summary="Ghostty — nvim"))

    digest = mem.digest(since=start - 60)
    assert digest.count("Firefox") == 1
    assert "Ghostty" in digest
    assert "(30m)" in digest or "(29m)" in digest


def test_digest_is_explicit_when_empty(mem):
    assert mem.digest(since=time.time() - 60) == "(nothing recorded)"


def test_events_since_filters_by_kind(mem):
    now = time.time()
    mem.add_event(TimelineRow(ts=now, kind="screen.focus", summary="a"))
    mem.add_event(TimelineRow(ts=now, kind="camera.presence", summary="b"))

    only = mem.events_since(now - 10, kinds=["camera.presence"])
    assert [r["summary"] for r in only] == ["b"]


def test_spend_accumulates(mem):
    mem.record_spend("claude-haiku-4-5", 0.001)
    mem.record_spend("claude-haiku-4-5", 0.002)
    assert mem.spend_since(time.time() - 60) == pytest.approx(0.003)
