"""The eval's own sanity: a badly-shaped scenario set would give a
comfortable-looking number that means nothing."""

from __future__ import annotations

from juno.evals.checkin import SCENARIOS, Result


def test_quiet_cases_outnumber_loud_ones():
    """Real check-ins are mostly nothing. If the set were balanced 50/50, a
    model that always speaks would score 50% and look merely mediocre rather
    than unusable."""
    quiet = [s for s in SCENARIOS if not s.should_speak]
    assert len(quiet) > len(SCENARIOS) / 2


def test_an_always_speaking_model_scores_badly():
    quiet = sum(1 for s in SCENARIOS if not s.should_speak)
    loud = sum(1 for s in SCENARIOS if s.should_speak)
    always_speaks = Result("stub", correct=loud, false_alarms=quiet)

    assert always_speaks.accuracy < 0.45
    assert always_speaks.false_alarms > always_speaks.correct


def test_an_always_silent_model_also_scores_badly():
    quiet = sum(1 for s in SCENARIOS if not s.should_speak)
    loud = sum(1 for s in SCENARIOS if s.should_speak)
    always_silent = Result("stub", correct=quiet, misses=loud)

    assert always_silent.accuracy < 0.7
    # But it produces no false alarms, which is the safer failure.
    assert always_silent.false_alarms == 0


def test_scenarios_have_distinct_names():
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))


def test_every_scenario_has_a_digest():
    assert all(s.digest.strip() for s in SCENARIOS)


def test_hard_cases_are_represented():
    """The set has to include the ones that look like the opposite of what they
    are, or it only measures the easy middle."""
    names = {s.name for s in SCENARIOS}
    assert "reading docs for the task" in names   # looks like drift, isn't
    assert "short break" in names                 # looks like drift, isn't
    assert "middle of the night" in names         # benign but for the hour


def test_accuracy_is_zero_for_an_empty_run():
    assert Result("stub").accuracy == 0.0
