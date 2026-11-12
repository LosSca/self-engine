"""用户可以在设置页填模型密钥，而不是只能改 .env。"""

from app.db import credential_store


def test_lists_every_provider_without_leaking_keys(client):
    resp = client.get("/api/providers")
    assert resp.status_code == 200
    rows = {row["id"]: row for row in resp.json()["providers"]}
    assert set(rows) == set(credential_store.PROVIDERS)
    for row in rows.values():
        assert "api_key" not in row


def test_saves_a_key_and_only_shows_the_tail(client):
    resp = client.post("/api/providers/deepseek", json={"api_key": "sk-test-12345678"})
    assert resp.status_code == 200
    row = next(r for r in resp.json()["providers"] if r["id"] == "deepseek")
    assert row["configured"] is True
    assert row["source"] == "settings"
    assert row["preview"].endswith("5678")
    assert "sk-test" not in str(resp.json())


def test_rejects_a_base_url_that_is_not_http(client):
    resp = client.post("/api/providers/local", json={"base_url": "127.0.0.1:8094"})
    assert resp.status_code == 400


def test_unknown_provider_is_404(client):
    assert client.post("/api/providers/nope", json={"api_key": "x"}).status_code == 404
    assert client.delete("/api/providers/nope").status_code == 404


def test_clearing_a_key_takes_the_environment_over_again(client, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client.post("/api/providers/deepseek", json={"api_key": "sk-live-abcdefgh"})
    resp = client.delete("/api/providers/deepseek")
    assert resp.status_code == 200
    row = next(r for r in resp.json()["providers"] if r["id"] == "deepseek")
    assert row["configured"] is False
    assert row["source"] == ""


def test_test_endpoint_reports_an_error_instead_of_raising(client, monkeypatch):
    async def boom(self, messages, **kwargs):
        raise RuntimeError("no key")

    from app.engines.llm_router import LLMRouter

    monkeypatch.setattr(LLMRouter, "complete", boom)
    resp = client.post("/api/providers/deepseek/test", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
    assert "no key" in resp.json()["error"]
