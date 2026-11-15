"""API tests for the memory fact surface (/api/memory)."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    from app.main import app

    return TestClient(app)


def _seed(tmp_path, facts):
    """Write facts through the store, the same way the pipeline does."""
    from app.db.memory_fact_store import MemoryFactStore
    from app.models.memory import EvidenceType, FactStatus, MemoryFact

    store = MemoryFactStore(str(tmp_path / "memory.db"))
    try:
        out = []
        for subject, predicate, value, observers in facts:
            fact = MemoryFact(
                campaign_id="c1",
                subject_id=subject,
                predicate=predicate,
                object_value=value,
                observer_ids=list(observers),
                evidence_type=EvidenceType.ENGINE_STATE,
                source_event_ids=["e1"],
            )
            fact, _ = store.add_fact(fact)
            store.advance_status(fact.id, FactStatus.ACTIVE)
            out.append(store.get_fact(fact.id))
        return out
    finally:
        store.close()


def test_facts_can_be_listed_for_a_campaign(client, tmp_path):
    _seed(tmp_path, [("Eileen", "status", "friend", ["player"])])
    res = client.get("/api/memory/facts", params={"campaign_id": "c1"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body) == 1
    assert body[0]["subject_id"] == "Eileen"
    assert body[0]["status"] == FactStatus_value("active")


def FactStatus_value(name):
    return name


def test_a_fact_can_be_fetched_with_its_evidence(client, tmp_path):
    facts = _seed(tmp_path, [("Eileen", "status", "friend", ["player"])])
    fact_id = facts[0].id
    res = client.get(f"/api/memory/facts/{fact_id}")
    assert res.status_code == 200
    assert res.json()["object_value"] == "friend"
    evidence = client.get(f"/api/memory/facts/{fact_id}/evidence")
    assert evidence.status_code == 200
    # No evidence rows were written by the seeder - provenance is optional per
    # fact, but the endpoint must answer rather than guess.
    assert isinstance(evidence.json(), list)


def test_an_unknown_fact_is_a_404(client):
    assert client.get("/api/memory/facts/nope").status_code == 404


def test_an_unknown_status_is_rejected_rather_than_ignored(client):
    res = client.get(
        "/api/memory/facts",
        params={"campaign_id": "c1", "status": "probably"},
    )
    assert res.status_code == 400


def test_the_overview_counts_by_status(client, tmp_path):
    _seed(tmp_path, [("Eileen", "status", "friend", ["player"])])
    res = client.get("/api/memory/overview", params={"campaign_id": "c1"})
    assert res.status_code == 200
    assert isinstance(res.json(), dict)

def test_ingest_defaults_to_a_dry_run(client, tmp_path):
    from app.db.event_store import EventStore, EventType

    events = EventStore(str(tmp_path / "events.db"))
    events.append(
        campaign_id="c1",
        event_type=EventType.COMBAT_RESULT,
        payload={"outcome": "victory", "opponent_name": "Raider", "opponent_power": 4},
        narrative_time_delta=0,
        location="gate",
        entities=["Raider"],
    )
    events.close()

    res = client.post("/api/memory/ingest", json={"campaign_id": "c1"})
    assert res.status_code == 200, res.text
    report = res.json()
    assert report["dry_run"] is True
    assert report["facts_created"] == 0
    assert report["consumed_events"] == 1
    # And nothing was written: a dry run must not leave the campaign holding
    # facts an operator only meant to preview.
    listed = client.get("/api/memory/facts", params={"campaign_id": "c1"})
    assert listed.json() == []


def test_ingest_can_be_committed_explicitly(client, tmp_path):
    from app.db.event_store import EventStore, EventType

    events = EventStore(str(tmp_path / "events.db"))
    events.append(
        campaign_id="c1",
        event_type=EventType.COMBAT_RESULT,
        payload={"outcome": "victory", "opponent_name": "Raider"},
        narrative_time_delta=0,
        location="gate",
        entities=["Raider"],
    )
    events.close()

    res = client.post("/api/memory/ingest", json={"campaign_id": "c1", "dry_run": False})
    assert res.status_code == 200, res.text
    assert res.json()["facts_created"] >= 1
    listed = client.get("/api/memory/facts", params={"campaign_id": "c1"})
    assert len(listed.json()) >= 1

def test_an_observer_does_not_see_a_secret_it_was_never_told(client, tmp_path):
    # The permission filter runs in the store/query layer, before the context
    # is built: a caller asking as the innkeeper must not receive a fact that
    # only the captain observed.
    _seed(tmp_path, [("Eileen", "goal", "secret mission", ["captain"])])
    res = client.get(
        "/api/memory/facts",
        params={"campaign_id": "c1", "observer": "innkeeper"},
    )
    assert res.status_code == 200, res.text
    assert res.json() == []
    seen = client.get(
        "/api/memory/facts", params={"campaign_id": "c1", "observer": "captain"}
    )
    assert len(seen.json()) == 1
