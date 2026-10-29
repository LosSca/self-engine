"""验收走查（项目验收标准 1-14）的 HTTP 版端到端测试。

每个 test 对应验收清单中的一条或几条，全部跑在真实的 TestClient + SQLite 上，
不走任何 mock 存储；唯一的替身是"叙事失败"与"LLM 生成"这一类外部服务。
"""
import pytest
from fastapi.testclient import TestClient

from app.api import routes_engine
from app.models.game import NarrativeResult
from tests.support import village_world_data

API = "/api/v1"


class FailingNarrator:
    """叙事服务不可用：行动结果仍必须提交。"""

    async def narrate(self, request, outcome, state, world, executions=(), quest_updates=()):
        return NarrativeResult(status="failed", error="narrator unavailable")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    # A local model server on this box must not make these assertions depend on
    # what a model chose to mention; the narrative is exercised separately.
    monkeypatch.setenv("ENGINE_NARRATOR", "template")
    from app.main import app

    return TestClient(app)


def _act(client, campaign_id, request_id, action, target=None):
    response = client.post(
        f"{API}/campaigns/{campaign_id}/actions",
        json={
            "campaign_id": campaign_id,
            "request_id": request_id,
            "action": action,
            "target_id": target,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_world(client, payload=None):
    response = client.post(f"{API}/worlds", json=payload or village_world_data())
    assert response.status_code == 200, response.text


def _bind(client, campaign_id):
    response = client.post(f"{API}/campaigns/{campaign_id}/world", json={"world_id": "demo_village"})
    assert response.status_code == 200, response.text
    return response.json()


def test_acceptance_world_key_door_quest_narrative_and_reload(client):
    # 1) 一个世界：村庄广场 + 森林边缘 + 仓库（含一扇上锁的门）
    created = client.post(f"{API}/worlds", json=village_world_data())
    assert created.status_code == 200, created.text
    world = client.get(f"{API}/worlds/demo_village").json()
    assert {loc["id"] for loc in world["locations"]} == {"village_square", "forest_edge", "warehouse"}
    assert world["doors"][0]["locked"] is True

    # 2) 一把钥匙，配在广场的地点上，拾取走 take 规则
    assert "iron_key" in world["locations"][0]["item_ids"]

    # 3) 玩家进入地点后能发现并拾取钥匙（look 能看到，take 成功）
    state = _bind(client, "camp-accept")
    assert state["location_id"] == "village_square"
    assert state["item_locations"]["iron_key"] == "village_square"
    looked = _act(client, "camp-accept", "acc-look", "look")
    assert looked["status"] == "applied"
    assert "Iron Key" in looked["narrative"]["text"]

    taken = _act(client, "camp-accept", "acc-take", "take", "iron_key")
    assert taken["status"] == "applied"
    assert taken["state"]["inventory"] == {"iron_key": 1}
    assert taken["state"]["quests"]["open_the_warehouse"]["objectives"]["find_key"] == 1

    # 4/5) 无钥匙被拒、有钥匙通过 —— 无钥匙用另一个 campaign 验证
    _bind(client, "camp-blind")
    refused = _act(client, "camp-blind", "blind-open", "open", "warehouse_door")
    assert refused["status"] == "rejected"
    assert refused["code"] == "door_locked"
    assert refused["state_version"] == 0
    assert refused["state"]["doors"]["warehouse_door"] is False
    assert "key" in refused["message"].lower()

    # 6) 按配置更新门状态，并记录事件（含 keeper_alert 事件执行）
    opened = _act(client, "camp-accept", "acc-open", "open", "warehouse_door")
    assert opened["status"] == "applied"
    assert opened["state"]["doors"]["warehouse_door"] is True
    assert opened["state"]["flags"]["warehouse_opened"] is True
    assert [execution["event_id"] for execution in opened["events_fired"]] == ["keeper_alert"]

    # 7) 任务进度得到正确更新并完成
    quest = opened["state"]["quests"]["open_the_warehouse"]
    assert quest["status"] == "completed"
    assert quest["objectives"] == {"find_key": 1, "open_door": 1}

    # 8) LLM（此处为确定性模板叙事）依据已确认结果生成叙事
    assert opened["narrative"]["status"] == "ok"
    assert "warehouse door" in opened["narrative"]["text"].lower()

    # 9) “重新加载存档”：全新 client 读取同一份 SQLite，状态一致
    from app.main import app

    with TestClient(app) as reloaded:
        reloaded_state = reloaded.get(f"{API}/campaigns/camp-accept/state").json()
    # look 也会推进一次状态版本，因此 take=2、open=3
    assert reloaded_state["version"] == opened["state_version"] == 3
    assert reloaded_state["inventory"] == {"iron_key": 1, "blessing_token": 1}
    assert reloaded_state["doors"]["warehouse_door"] is True
    assert reloaded_state["quests"]["open_the_warehouse"]["status"] == "completed"


def test_acceptance_duplicate_requests_do_not_repeat_side_effects(client):
    _create_world(client)
    _bind(client, "camp-dup")

    first = _act(client, "camp-dup", "dup-take", "take", "iron_key")
    assert first["status"] == "applied"
    replay = _act(client, "camp-dup", "dup-take", "take", "iron_key")
    assert replay["status"] == "duplicate"
    assert replay["state_version"] == first["state_version"] == 1

    opened = _act(client, "camp-dup", "dup-open", "open", "warehouse_door")
    opened_replay = _act(client, "camp-dup", "dup-open", "open", "warehouse_door")
    assert opened_replay["status"] == "duplicate"

    state = client.get(f"{API}/campaigns/camp-dup/state").json()
    # 钥匙没有被重复拾取，奖励没有重复发放
    assert state["inventory"] == {"iron_key": 1, "blessing_token": 1}
    assert state["version"] == opened["state_version"] == 2
    # 域事件只在真正提交时写入：重放不产生新事件
    events = client.get(f"{API}/campaigns/camp-dup/events").json()
    versions = [event["state_version"] for event in events]
    assert sorted(versions) == versions
    assert max(versions) == 2


def test_acceptance_narrative_failure_keeps_committed_state(client, monkeypatch):
    _create_world(client)
    _bind(client, "camp-nofx")
    monkeypatch.setattr(routes_engine, "_get_narrator", lambda: FailingNarrator())

    result = _act(client, "camp-nofx", "nofx-take", "take", "iron_key")
    assert result["status"] == "applied"
    assert result["narrative"]["status"] == "failed"
    assert "narrator unavailable" in result["narrative"]["error"]

    state = client.get(f"{API}/campaigns/camp-nofx/state").json()
    assert state["version"] == 1
    assert state["inventory"] == {"iron_key": 1}
    events = [event["event_type"] for event in client.get(f"{API}/campaigns/camp-nofx/events").json()]
    assert "narrative.failed" in events


def test_acceptance_invalid_event_and_simulation_is_read_only(client):
    # 效果引用了一个不存在的物品：语义非法，必须给出可读错误而不是 500
    payload = village_world_data()
    payload["events"][0]["effects"][0] = {"kind": "give_item", "target": "ghost_relic", "amount": 1}
    rejected = client.post(f"{API}/worlds", json=payload)
    assert rejected.status_code == 400
    errors = rejected.json()["detail"]
    assert any("ghost_relic" in error for error in errors), errors

    validation = client.post(f"{API}/worlds/validate", json=payload)
    assert validation.json()["valid"] is False
    assert client.get(f"{API}/worlds").json() == []

    _create_world(client)
    _bind(client, "camp-sim")
    before = client.get(f"{API}/campaigns/camp-sim/state").json()

    simulated = client.post(f"{API}/worlds/demo_village/events/keeper_alert/simulate?campaign_id=camp-sim")
    assert simulated.status_code == 200, simulated.text
    body = simulated.json()
    assert body["status"] in ("not_applicable", "would_fire")

    after = client.get(f"{API}/campaigns/camp-sim/state").json()
    assert after == before
    assert after["version"] == 0
    assert after["doors"]["warehouse_door"] is False
    assert "warehouse_opened" not in after["flags"]
