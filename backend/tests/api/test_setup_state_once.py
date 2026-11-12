"""开局的初始化问题只应问一次。

两个曾把玩家锁在向导里的缺陷都出在「答案没保存」：
1. 向导的最后一问与角色舞台同时存在时，isLast 判成 false，按钮只是切步不提交；
2. 提交时读到 React 还没刷新的旧 answers，POST 的 body 是 {}。

后端这一半的约定：只要 setup_answers 非空，setup-state 就不再要求初始化。
"""

from tests.conftest import make_scenario


def _scenario_with_question(client):
    return make_scenario(
        client,
        title="初始化一次就够",
        setup_questions=[
            {"id": "q1", "var_name": "who", "prompt": "你是谁？", "type": "text", "required": True},
        ],
    )


def test_setup_state_stops_asking_once_answers_are_saved(client):
    scenario = _scenario_with_question(client)
    campaign = client.post(
        f"/api/scenarios/{scenario['id']}/campaigns", json={"player_name": "旅人"},
    ).json()
    campaign_id = campaign.get("id") or campaign.get("campaign_id")

    before = client.get(f"/api/game/{campaign_id}/setup-state").json()
    assert before["needs_setup"] is True

    saved = client.post(
        f"/api/game/{campaign_id}/setup-answers",
        json={"answers": {"who": {"var_name": "who", "type": "text", "value": "抄书人"}}},
    )
    assert saved.status_code == 200

    after = client.get(f"/api/game/{campaign_id}/setup-state").json()
    assert after["needs_setup"] is False
    assert after["answers"]["who"]["value"] == "抄书人"


def test_an_empty_answer_set_keeps_the_wizard_open(client):
    """存了空对象等于没存：玩家刷新后还得能重新答。"""
    scenario = _scenario_with_question(client)
    campaign = client.post(
        f"/api/scenarios/{scenario['id']}/campaigns", json={"player_name": "旅人"},
    ).json()
    campaign_id = campaign.get("id") or campaign.get("campaign_id")

    client.post(f"/api/game/{campaign_id}/setup-answers", json={"answers": {}})

    state = client.get(f"/api/game/{campaign_id}/setup-state").json()
    assert state["needs_setup"] is True