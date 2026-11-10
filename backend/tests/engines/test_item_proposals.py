"""Item proposals are the migration path away from prose-authored inventory.

The rules engine is the authority for item state; narration may only *propose*.
These tests pin the boundary: tags never reach the player, proposals are
validated data, duplicates do not multiply, and nothing here mutates state.
"""
import pytest

from app.engines.item_proposals import (
    ACTIONS,
    MAX_CATEGORY,
    MAX_NAME,
    MAX_SOURCE,
    ItemProposal,
    ProposalBatch,
    dedupe,
    describe,
    make_proposal,
    proposals_from_text,
    validate_proposal,
)


def test_known_actions_are_stable():
    assert ACTIONS == ("add", "use", "lose")


def test_make_proposal_normalizes_fields():
    proposal = make_proposal("ADD", "  Rusty   Key ", " key ", " narrator ", turn=3)
    assert proposal.action == "add"
    assert proposal.name == "Rusty   Key"
    assert proposal.category == "key"
    assert proposal.source == "narrator"
    assert proposal.turn == 3
    assert proposal.key == "3:add:rusty   key"


def test_unknown_action_falls_back_to_add_instead_of_raising():
    proposal = make_proposal("destroy", "Lantern")
    assert proposal.action == "add"


def test_control_characters_are_scrubbed_from_every_field():
    proposal = make_proposal("add", "key\x00\x1f", "cat\x07", "src\ntext")
    assert "\x00" not in proposal.name and "\x1f" not in proposal.name
    assert "\x07" not in proposal.category
    assert "\n" not in proposal.source


def test_fields_are_capped():
    proposal = make_proposal("add", "n" * 500, "c" * 500, "s" * 500)
    assert len(proposal.name) == MAX_NAME
    assert len(proposal.category) == MAX_CATEGORY
    assert len(proposal.source) == MAX_SOURCE


def test_empty_category_and_source_get_defaults():
    proposal = make_proposal("add", "Flask", "", "   ")
    assert proposal.category == "misc"
    assert proposal.source == "narrator"


@pytest.mark.parametrize(
    "proposal, expected",
    [
        (make_proposal("add", ""), "物品名为空"),
        (ItemProposal(action="add", name="n" * (MAX_NAME + 1)), "物品名过长"),
        (make_proposal("add", "   "), "物品名为空"),
    ],
)
def test_validate_proposal_reports_readable_reasons(proposal, expected):
    assert expected in validate_proposal(proposal)


def test_validate_proposal_accepts_a_well_formed_proposal():
    assert validate_proposal(make_proposal("use", "Lantern")) == ""


def test_unknown_action_is_rejected_with_the_chinese_reason():
    bad = ItemProposal(action="destroy", name="Lantern")
    assert validate_proposal(bad) == "未知的物品操作：'destroy'"


def test_proposals_from_text_strips_tags_from_prose():
    text = (
        "You spot a lantern. [ITEM_ADD:Brass Lantern|light|narrator] "
        "It flickers. [ITEM_USE:Torch] and then [ITEM_LOSE:Old Map]."
    )
    clean, proposals = proposals_from_text(text, turn=7)
    assert "[ITEM_" not in clean
    assert "You spot a lantern." in clean
    assert [(p.action, p.name) for p in proposals] == [
        ("add", "Brass Lantern"),
        ("use", "Torch"),
        ("lose", "Old Map"),
    ]
    assert all(p.turn == 7 for p in proposals)
    assert proposals[0].category == "light"
    assert proposals[0].source == "narrator"


def test_proposals_from_text_handles_plain_prose():
    clean, proposals = proposals_from_text("Nothing happens.", turn=1)
    assert clean == "Nothing happens."
    assert proposals == []


def test_proposals_from_text_ignores_malformed_tags_but_keeps_text():
    text = "Careful [ITEM_ADD:no separator] here."
    clean, proposals = proposals_from_text(text)
    assert proposals == []
    assert "[ITEM_ADD:no separator]" in clean


def test_batch_deduplicates_by_key_and_counts_duplicates():
    batch = ProposalBatch()
    assert batch.add(make_proposal("add", "Key", turn=1)) is True
    assert batch.add(make_proposal("add", "key", turn=1)) is False
    assert batch.add(make_proposal("add", "Key", turn=2)) is True
    assert len(batch.proposals) == 2
    assert batch.duplicates == 1


def test_dedupe_builds_a_batch_from_a_stream():
    batch = dedupe(
        [
            make_proposal("add", "Key", turn=1),
            make_proposal("add", "Key", turn=1),
            make_proposal("lose", "Key", turn=1),
        ]
    )
    assert len(batch.proposals) == 2
    assert batch.duplicates == 1


def test_payloads_carry_only_validated_fields():
    payload = make_proposal("add", "Key", "key", "narrator", turn=4).as_event_payload()
    assert set(payload) == {"action", "name", "category", "source", "turn", "status", "reason"}
    assert payload["status"] == "proposed"
    assert payload["reason"] == ""


def test_describe_renders_a_readable_summary():
    batch = dedupe([make_proposal("add", "Key"), make_proposal("lose", "Map")])
    assert describe(batch.proposals) == "add:Key, lose:Map"


def test_proposals_never_touch_state():
    """Pure data: importing and parsing has no side effects on any store."""
    import inspect

    from app.engines import item_proposals

    source = inspect.getsource(item_proposals)
    for forbidden in ("sqlite3", "GameStateStore", "state.inventory", "open("):
        assert forbidden not in source
