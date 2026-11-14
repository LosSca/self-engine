"""Cross-browser preferences and partial play-config writes.

设置以前只活在前端 localStorage，换机器就丢；这组用例锁住后端那份唯一的真相，
并确认 API 令牌永远不会被后端回传。
"""

from fastapi.testclient import TestClient


def test_prefs_round_trip_keeps_only_allowed_keys(client):
    r = client.post(
        "/api/prefs",
        json={
            "temperature": 1.1,
            "theme": "dark",
            "default_narrator_prompt": "写得冷一点。",
            "api_token": "should-not-be-stored",
        },
    )
    assert r.status_code == 200
    stored = r.json()["prefs"]
    assert stored["temperature"] == 1.1
    assert stored["theme"] == "dark"
    assert stored["default_narrator_prompt"] == "写得冷一点。"
    assert "api_token" not in stored
    assert client.get("/api/prefs").json()["prefs"] == stored


def test_prefs_rejects_a_write_without_known_keys(client):
    r = client.post("/api/prefs", json={"api_token": "secret"})
    assert r.status_code == 400


def test_settings_endpoint_reads_back_the_global_directive(client):
    client.post("/api/prefs", json={"default_narrator_prompt": "短句。"})
    body = client.get("/api/settings").json()
    assert body["default_narrator_prompt"] == "短句。"
    assert body["prefs"]["default_narrator_prompt"] == "短句。"


def test_play_config_accepts_a_partial_write_of_the_directive_only(client):
    created = client.post(
        "/api/scenarios/",
        json={"title": "雨夜", "opening_narrative": "雨。", "language": "zh"},
    )
    scenario_id = created.json()["id"]
    before = client.get(f"/api/v1/scenarios/{scenario_id}/play-config").json()

    r = client.put(
        f"/api/v1/scenarios/{scenario_id}/play-config",
        json={"narrator_prompt": "这个世界的雨永远不会停。"},
    )
    assert r.status_code == 200
    after = r.json()
    assert after["narrator_prompt"] == "这个世界的雨永远不会停。"
    # 没传的字段必须原样保留，不能被默认值覆盖。
    assert after["mode"] == before["mode"]
    assert after["max_depth"] == before["max_depth"]
    assert after["context_budget_chars"] == before["context_budget_chars"]
