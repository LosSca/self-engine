import pytest

from app.models.work import CharacterProfile, WorkProfile
from app.services import work_catalogue


def _char(name: str = "无名者") -> CharacterProfile:
    return CharacterProfile(name=name, role="旅人", description="测试用角色", traits=["谨慎"])


def test_every_curated_work_becomes_a_valid_world():
    from app.services import world_service

    assert len(work_catalogue.catalogue_keys()) >= 6
    for key in work_catalogue.catalogue_keys():
        profile = work_catalogue.find_work(key)
        assert profile is not None, key
        character = character = work_catalogue.character_catalogue(profile)[0]
        char = CharacterProfile(**character)
        definition = work_catalogue.seed_world_definition(profile, char, "w_" + key)
        world_service.validate_world(definition)


def test_seeded_world_is_fully_reachable():
    profile = work_catalogue.find_work("三体")
    char = CharacterProfile(**work_catalogue.character_catalogue(profile)[0])
    definition = work_catalogue.seed_world_definition(profile, char, "w_three")
    edges = {loc.id: [exit_.target_id for exit_ in loc.exits] for loc in definition.locations}
    seen = {definition.start_location_id}
    frontier = [definition.start_location_id]
    while frontier:
        node = frontier.pop()
        for nxt in edges.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert seen == set(edges)


def test_key_for_prefers_the_catalogue_key_over_the_title():
    assert work_catalogue.key_for("福尔摩斯") == "sherlock_holmes"
    assert work_catalogue.key_for("sherlock_holmes") == "sherlock_holmes"
    # Unknown CJK titles have no ASCII form; callers must de-duplicate.
    assert work_catalogue.key_for("一个不存在的作品") == "entry"
    assert work_catalogue.key_for("Unknown Work") == "unknown_work"


def test_slug_of_rejects_non_ascii_openings():
    assert work_catalogue.slug_of("abc def") == "abc_def"
    assert work_catalogue.slug_of("三体") == "entry"
    assert work_catalogue.slug_of("") == "entry"


def test_profile_from_payload_rejects_unusable_model_output():
    with pytest.raises(ValueError):
        work_catalogue.profile_from_payload("not a dict")
    with pytest.raises(ValueError):
        work_catalogue.profile_from_payload({"title": "", "characters": []})
    with pytest.raises(ValueError):
        work_catalogue.profile_from_payload({"title": "X", "characters": "nope"})
    with pytest.raises(ValueError):
        work_catalogue.profile_from_payload({"title": "X", "characters": []})


def test_profile_from_payload_fills_missing_fields():
    profile = work_catalogue.profile_from_payload(
        {"title": "测试作品", "characters": [{"name": "甲", "role": "信使"}]},
        source="generated",
    )
    assert profile.title == "测试作品"
    assert profile.source == "generated"
    assert profile.characters[0].name == "甲"
    assert profile.locations
