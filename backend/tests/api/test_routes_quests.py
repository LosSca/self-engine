import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("ENGINE_DEBUG_TOOLS", "1")
    from app.main import app

    return TestClient(app)


V1 = "/api/v1"


def _world(client, world_id="w_quest") -> None:
    """Create one world with an item, a door, a location and no quests yet."""
    body = {
        "world_id": world_id,
        "name": "任务测试世界",
        "description": "",
        "start_location_id": "village",
        "locations": [
            {"id": "village", "name": "村庄", "description": "", "item_ids": ["brass_key"]},
            {"id": "depot", "name": "仓库", "description": ""},
        ],
        "items": [{"id": "brass_key", "name": "铜钥匙"}],
        "doors": [
            {
                "id": "depot_door",
                "name": "仓库铁门",
                "location_id": "village",
                "locked": True,
                "key_item_id": "brass_key",
            }
        ],
    }
    res = client.post(f"{V1}/worlds", json=body)
    assert res.status_code in (200, 201), res.text


def test_quest_rest_lifecycle(client):
    _world(client)

    # Empty list before anything is authored.
    assert client.get(f"{V1}/worlds/w_quest/quests").json() == []

    quest = {
        "id": "find_and_open",
        "title": "找到钥匙并打开仓库",
        "objectives": [
            {"id": "get_key", "kind": "obtain_item", "target": "brass_key"},
            {"id": "open_depot", "kind": "open_door", "target": "depot_door"},
        ],
        "rewards": [{"kind": "add_xp", "amount": 30}],
    }
    res = client.post(f"{V1}/worlds/w_quest/quests", json=quest)
    assert res.status_code == 201, res.text
    assert [q["id"] for q in res.json()["quests"]] == ["find_and_open"]

    got = client.get(f"{V1}/worlds/w_quest/quests/find_and_open")
    assert got.status_code == 200
    assert [o["id"] for o in got.json()["objectives"]] == ["get_key", "open_depot"]

    # Adding one objective keeps the others and the rewards untouched.
    res = client.put(
        f"{V1}/worlds/w_quest/quests/find_and_open/objectives/reach_depot",
        json={"id": "reach_depot", "kind": "reach_location", "target": "depot"},
    )
    assert res.status_code == 200, res.text
    quest_after = next(q for q in res.json()["quests"] if q["id"] == "find_and_open")
    assert [o["id"] for o in quest_after["objectives"]] == [
        "get_key",
        "open_depot",
        "reach_depot",
    ]
    assert quest_after["rewards"] == [
        {"kind": "add_xp", "target": "", "value": None, "amount": 30}
    ]

    # A reward row is addressed by its effect kind.
    res = client.put(
        f"{V1}/worlds/w_quest/quests/find_and_open/rewards/give_item",
        json={"kind": "give_item", "target": "brass_key"},
    )
    assert res.status_code == 200, res.text

    res = client.patch(
        f"{V1}/worlds/w_quest/quests/find_and_open",
        json={
            "id": "find_and_open",
            "title": "找到钥匙并打开仓库",
            "description": "找到铜钥匙，打开仓库铁门。",
            "objectives": quest_after["objectives"],
            "rewards": quest_after["rewards"],
        },
    )
    assert res.status_code == 200, res.text

    res = client.delete(f"{V1}/worlds/w_quest/quests/find_and_open/objectives/reach_depot")
    assert res.status_code == 200, res.text
    quest_after = next(q for q in res.json()["quests"] if q["id"] == "find_and_open")
    assert [o["id"] for o in quest_after["objectives"]] == ["get_key", "open_depot"]

    no_reward = client.delete(f"{V1}/worlds/w_quest/quests/find_and_open/rewards/give_item")
    assert no_reward.status_code == 404, no_reward.text
    no_reward = client.delete(f"{V1}/worlds/w_quest/quests/find_and_open/rewards/add_xp")
    assert no_reward.status_code == 200

    res = client.delete(f"{V1}/worlds/w_quest/quests/find_and_open")
    assert res.status_code == 200
    assert res.json()["quests"] == []


def test_quest_edits_fail_readably(client):
    _world(client)
    client.post(
        f"{V1}/worlds/w_quest/quests",
        json={
            "id": "q1",
            "title": "Q1",
            "objectives": [{"id": "o1", "kind": "obtain_item", "target": "brass_key"}],
        },
    )

    # A body id that disagrees with the path is an authoring error, not a rename.
    bad = client.patch(f"{V1}/worlds/w_quest/quests/other", json={"id": "q1", "title": "Q1"})
    assert bad.status_code == 400, bad.text

    # An objective pointing at an unknown door fails world validation.
    bad = client.put(
        f"{V1}/worlds/w_quest/quests/q1/objectives/o2",
        json={"id": "o2", "kind": "open_door", "target": "ghost_door"},
    )
    assert bad.status_code == 400, bad.text
    assert "ghost_door" in bad.text

    # The rejected edit left the stored world alone.
    got = client.get(f"{V1}/worlds/w_quest/quests/q1")
    assert [o["id"] for o in got.json()["objectives"]] == ["o1"]

    missing = client.delete(f"{V1}/worlds/w_quest/quests/q1/objectives/nope")
    assert missing.status_code == 404, missing.text

    unknown_quest = client.get(f"{V1}/worlds/w_quest/quests/nope")
    assert unknown_quest.status_code == 404


def test_world_quest_progress_is_read_from_the_state(client):
    """A brand new campaign reports authored quests with zero progress."""
    _world(client)
    client.post(
        f"{V1}/worlds/w_quest/quests",
        json={
            "id": "find_and_open",
            "title": "找到钥匙并打开仓库",
            "objectives": [
                {"id": "get_key", "kind": "obtain_item", "target": "brass_key"},
                {"id": "open_depot", "kind": "open_door", "target": "depot_door"},
            ],
        },
    )
    campaign = "c_quest"
    res = client.post(
        f"{V1}/campaigns/{campaign}/world", json={"world_id": "w_quest"}
    )
    assert res.status_code in (200, 201), res.text

    body = client.get(f"{V1}/campaigns/{campaign}/quest-progress").json()
    quest = next(q for q in body["quests"] if q["quest_id"] == "find_and_open")
    assert quest["status"] == "active"
    assert quest["completed_objectives"] == 0
    assert quest["total_objectives"] == 2
    assert [o["count"] for o in quest["objectives"]] == [0, 0]
    assert body["active"] == ["find_and_open"]


def _campaign_with_quest(client, campaign="c_manual") -> None:
    """One bound campaign whose quest needs one item and one door."""
    _world(client)
    client.post(
        f"{V1}/worlds/w_quest/quests",
        json={
            "id": "find_and_open",
            "title": "找到钥匙并打开仓库",
            "objectives": [
                {"id": "get_key", "kind": "obtain_item", "target": "brass_key"},
                {"id": "open_depot", "kind": "open_door", "target": "depot_door"},
            ],
            "rewards": [{"kind": "add_xp", "amount": 30}],
        },
    )
    res = client.post(f"{V1}/campaigns/{campaign}/world", json={"world_id": "w_quest"})
    assert res.status_code in (200, 201), res.text


def test_manual_progress_commits_through_the_action_service(client):
    """An authored nudge is a real commit: new version, domain events, quest move."""
    _campaign_with_quest(client)
    url = f"{V1}/campaigns/c_manual/quests/find_and_open/objectives/get_key/progress"
    res = client.post(url, json={"amount": 1, "request_id": "manual-1"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "applied"
    # The binding turn is already version 1; the nudge is the next one.
    assert body["state_version"] >= 1
    assert body["quests"]["find_and_open"] == "active"

    quest = next(
        q for q in client.get(f"{V1}/campaigns/c_manual/quest-progress").json()["quests"]
        if q["quest_id"] == "find_and_open"
    )
    assert quest["objectives"][0]["count"] == 1
    assert quest["objectives"][0]["done"] is True
    assert quest["objectives"][1]["count"] == 0

    kinds = [row["event_type"] for row in client.get(f"{V1}/campaigns/c_manual/events").json()]
    assert "debug.asserted" in kinds
    assert "quest.progressed" in kinds


def test_manual_progress_is_idempotent(client):
    """The same request id never advances an objective twice."""
    _campaign_with_quest(client)
    url = f"{V1}/campaigns/c_manual/quests/find_and_open/objectives/get_key/progress"
    first = client.post(url, json={"amount": 1, "request_id": "same"})
    assert first.status_code == 200, first.text
    second = client.post(url, json={"amount": 1, "request_id": "same"})
    assert second.status_code == 200, second.text
    assert second.json()["state_version"] == first.json()["state_version"]
    assert second.json()["request_id"] == "same"
    quest = next(
        q for q in client.get(f"{V1}/campaigns/c_manual/quest-progress").json()["quests"]
        if q["quest_id"] == "find_and_open"
    )
    assert quest["objectives"][0]["count"] == 1


def test_manual_progress_reports_unknown_ids(client):
    _campaign_with_quest(client)
    base = f"{V1}/campaigns/c_manual/quests"
    assert client.post(f"{base}/nope/objectives/get_key/progress", json={}).status_code == 404
    assert client.post(f"{base}/find_and_open/objectives/nope/progress", json={}).status_code == 404
    bad = client.post(f"{base}/find_and_open/objectives/get_key/progress", json={"amount": 0})
    assert bad.status_code == 422, bad.text


def test_manual_progress_finishes_the_quest_and_pays_rewards_once(client):
    """Completing through the tool follows the same reward path as a real turn."""
    _campaign_with_quest(client)
    base = f"{V1}/campaigns/c_manual/quests/find_and_open/objectives"
    for objective, rid in (("get_key", "m1"), ("open_depot", "m2")):
        res = client.post(f"{base}/{objective}/progress", json={"amount": 1, "request_id": rid})
        assert res.status_code == 200, res.text
    done = next(
        q for q in client.get(f"{V1}/campaigns/c_manual/quest-progress").json()["quests"]
        if q["quest_id"] == "find_and_open"
    )
    assert done["status"] == "completed"

    xp = client.get(f"{V1}/campaigns/c_manual/state").json()["xp"]
    assert xp == 30

    replay = client.post(f"{base}/open_depot/progress", json={"amount": 1, "request_id": "m2"})
    assert replay.status_code in (200, 409), replay.text
    assert client.get(f"{V1}/campaigns/c_manual/state").json()["xp"] == xp


def test_manual_progress_refuses_when_debug_tools_are_off(client, monkeypatch):
    """Production installs do not expose a state-changing tool."""
    monkeypatch.setenv("ENGINE_DEBUG_TOOLS", "0")
    _campaign_with_quest(client)
    res = client.post(
        f"{V1}/campaigns/c_manual/quests/find_and_open/objectives/get_key/progress",
        json={},
    )
    assert res.status_code == 403, res.text
