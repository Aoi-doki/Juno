"""Tests for the parts of the voice pipeline that don't need a microphone.

The timing and chunking rules are where the bugs actually are — whether she
gets cut off mid-word, whether a pause mid-sentence ends your turn early — and
all of it is pure logic, so none of it needs audio hardware to verify.
"""

from __future__ import annotations

import itertools

import pytest

from juno_laptop.link import backoff_delays
from juno_laptop.listen import Endpointer
from juno_laptop.speak import split_sentences


class TestSplitSentences:
    def test_a_short_opening_merges_into_the_next_sentence(self):
        """"Morning." alone would be its own synthesis pass, and she'd stutter
        before the real sentence started."""
        assert split_sentences("Morning. You have a call at eleven.") == [
            "Morning. You have a call at eleven."
        ]

    def test_a_lone_short_reply_still_gets_spoken(self):
        assert split_sentences("Yes.") == ["Yes."]

    def test_several_short_openers_collapse_together(self):
        assert split_sentences("Hey. Right. Time to stop scrolling and go to bed.") == [
            "Hey. Right. Time to stop scrolling and go to bed."
        ]

    def test_long_sentences_stay_separate(self):
        chunks = split_sentences(
            "That is forty minutes on Reddit today. You said you were writing the report."
        )
        assert len(chunks) == 2
        assert chunks[0].endswith("today.")

    def test_short_fragments_merge_backwards(self):
        """'Yes. OK.' must not become two synthesis passes — it stutters."""
        assert split_sentences("You have been at this for an hour already. Yes. Really.") == [
            "You have been at this for an hour already. Yes. Really."
        ]

    def test_question_and_exclamation_split(self):
        chunks = split_sentences(
            "Are you actually going to finish that today? Because it does not look like it."
        )
        assert len(chunks) == 2

    def test_whitespace_and_newlines_are_normalised(self):
        assert split_sentences("  hello   there\n\nfriend  ") == ["hello there friend"]

    @pytest.mark.parametrize("empty", ["", "   ", "\n\n"])
    def test_empty_input_yields_nothing(self, empty):
        assert split_sentences(empty) == []

    def test_text_without_punctuation_is_one_chunk(self):
        assert split_sentences("no punctuation here at all") == ["no punctuation here at all"]


class TestEndpointer:
    def test_silence_after_speech_ends_the_utterance(self):
        ep = Endpointer(silence_ms=100, max_seconds=10, frame_ms=20)  # 5 silent frames
        assert ep.feed(True) is False
        for _ in range(4):
            assert ep.feed(False) is False
        assert ep.feed(False) is True

    def test_leading_silence_does_not_end_it(self):
        """Someone who breathes after the wake word must not be cut off."""
        ep = Endpointer(silence_ms=60, max_seconds=10, frame_ms=20)
        for _ in range(50):
            assert ep.feed(False) is False
        assert ep.heard_speech is False

    def test_a_pause_mid_sentence_does_not_end_it(self):
        ep = Endpointer(silence_ms=200, max_seconds=10, frame_ms=20)  # 10 frames
        ep.feed(True)
        for _ in range(9):
            assert ep.feed(False) is False
        ep.feed(True)  # they carried on
        for _ in range(9):
            assert ep.feed(False) is False

    def test_max_duration_stops_a_pinned_microphone(self):
        """A noisy room must not hold the microphone open forever."""
        ep = Endpointer(silence_ms=10_000, max_seconds=0.1, frame_ms=20)  # 5 frames
        results = [ep.feed(True) for _ in range(5)]
        assert results[-1] is True

    def test_reset_clears_state(self):
        ep = Endpointer(silence_ms=40, max_seconds=10, frame_ms=20)
        ep.feed(True)
        ep.reset()
        assert ep.heard_speech is False
        for _ in range(2):
            assert ep.feed(False) is False


class TestBackoff:
    def test_doubles_then_caps(self):
        delays = list(itertools.islice(backoff_delays(1.0, 16.0), 8))
        assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 16.0, 16.0, 16.0]

    def test_never_exceeds_the_cap(self):
        assert all(d <= 60.0 for d in itertools.islice(backoff_delays(), 100))
