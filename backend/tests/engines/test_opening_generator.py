"""The AI cold-open is the first prose a player sees, and it runs before any
state exists. These tests cover the deterministic parts: setup formatting,
sample answers, and the truncated-output cleanup.
"""
from types import SimpleNamespace

import pytest

from app.engines.opening_generator import (
    _clean_truncated_opening,
    format_setup_lines,
    generate_opening,
    synthesize_sample_answers,
)


def test_complete_text_is_left_alone():
    text = "You wake in the hold. The lamp is out."
    assert _clean_truncated_opening(text) == text


@pytest.mark.parametrize("ending", [".", "!", "?", "…", '"', "”", "»", ")"])
def test_terminal_punctuation_is_respected(ending):
    text = "You wake" + ending
    assert _clean_truncated_opening(text) == text


def test_mid_sentence_cut_is_trimmed_to_the_last_full_stop():
    text = "You wake in the hold. The lamp is out and your hands are co"
    assert _clean_truncated_opening(text) == "You wake in the hold."


def test_cut_inside_the_first_quarter_is_not_aggressively_trimmed():
    # Too little complete prose to be worth keeping; better to show what we have.
    text = "Ok. And then the whole rest of the sentence runs on and on and o"
    assert _clean_truncated_opening(text) == text


def test_empty_input_stays_empty():
    assert _clean_truncated_opening("") == ""


def test_closing_quote_after_a_full_stop_counts_as_complete():
    text = 'He says "run." And then the door'
    assert _clean_truncated_opening(text) == 'He says "run."'


def test_format_setup_lines_follows_the_question_order():
    answers = {
        "creed": {"var_name": "creed", "value": "Never lie"},
        "name": {"var_name": "name", "value": "Wren"},
    }
    questions = [{"var_name": "name"}, {"var_name": "creed"}]
    lines = format_setup_lines(answers, questions)
    assert lines == ["- name: Wren", "- creed: Never lie"]


def test_format_setup_lines_keeps_trailing_answers():
    answers = {
        "name": {"var_name": "name", "value": "Wren"},
        "extra": {"var_name": "extra", "value": "A scar"},
    }
    lines = format_setup_lines(answers, [{"var_name": "name"}])
    assert lines == ["- name: Wren", "- extra: A scar"]


def test_format_setup_lines_includes_descriptions_on_their_own_line():
    answers = {
        "name": {"var_name": "name", "value": "Wren", "description": "the lookout"},
    }
    assert format_setup_lines(answers) == ["- name: Wren\n  (the lookout)"]


def test_format_setup_lines_skips_empty_and_malformed_answers():
    answers = {
        "empty": {"var_name": "empty", "value": "   "},
        "not_a_dict": "plain string",
        "kept": {"var_name": "kept", "value": "yes"},
    }
    assert format_setup_lines(answers) == ["- kept: yes"]


def test_format_setup_lines_on_no_answers():
    assert format_setup_lines({}) == []
    assert format_setup_lines(None) == []


def test_synthesize_sample_answers_picks_the_first_choice():
    questions = [
        {"var_name": "origin", "type": "choice", "options": [
            {"label": "Fisher", "description": "salty hands"},
            {"label": "Soldier"},
        ]},
        {"var_name": "call_sign", "type": "text"},
        {"type": "text"},
    ]
    sample = synthesize_sample_answers(questions)
    assert sample["origin"] == {
        "var_name": "origin",
        "type": "choice",
        "value": "Fisher",
        "description": "salty hands",
    }
    assert sample["call_sign"]["value"] == "Call Sign"
    assert len(sample) == 2


def test_synthesize_sample_answers_falls_back_for_empty_options():
    sample = synthesize_sample_answers([{"var_name": "x", "type": "choice", "options": []}])
    assert sample["x"]["value"] == "X"


@pytest.mark.asyncio
async def test_generate_opening_sends_tone_lore_and_setup_to_the_router():
    captured = {}

    class Router:
        async def complete(self, messages, max_tokens=0):
            captured["messages"] = messages
            captured["max_tokens"] = max_tokens
            return "  You wake in the dark.  "

    text = await generate_opening(
        language="en",
        tone="grim",
        lore="the moon fell",
        character_setup_lines=["- name: Wren"],
        director_note="start with rain",
        router=Router(),
        max_tokens=321,
    )
    assert text == "You wake in the dark."
    assert captured["max_tokens"] == 321
    system, user = captured["messages"]
    assert system["role"] == "system" and "cold-open" in system["content"]
    assert "grim" in user["content"]
    assert "the moon fell" in user["content"]
    assert "- name: Wren" in user["content"]
    assert "start with rain" in user["content"]


@pytest.mark.asyncio
async def test_generate_opening_marks_missing_inputs_as_unspecified():
    captured = {}

    class Router:
        async def complete(self, messages, max_tokens=0):
            captured["user"] = messages[1]["content"]
            return ""

    assert await generate_opening(
        language="en", tone="", lore="", character_setup_lines=[], router=Router()
    ) == ""
    assert "(unspecified)" in captured["user"]
    assert "(none provided)" in captured["user"]
    assert "DIRECTOR'S NOTE" not in captured["user"]
