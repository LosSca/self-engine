"""World evolution: proposals, validation, and the audit trail (Phase 3)."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenario.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    from app.main import app

    return TestClient(app)


@pytest.fixture
def world_id(client):
    from app.api import routes_engine
    from app.models.game import LocationDef, WorldDefinition

    world = WorldDefinition(
        world_id="evo_world",
        name="Village",
        start_location_id="square",
        locations=[
            LocationDef(id="square", name="Square"),
            LocationDef(id="chapel", name="Old Chapel"),
        ],
    )
    store = routes_engine._get_store()
    try:
        store.save_world(world)
    finally:
        store.close()
    return "evo_world"


def _propose(client, world_id, **body):
    payload = {"text": "", "commit": False}
    payload.update(body)
    return client.post("/api/v1/worlds/{0}/evolution/propose".format(world_id), json=payload)


def test_propose_is_a_dry_run_by_default(client, world_id):
    from app.api import routes_engine

    response = _propose(
        client, world_id, text="There is an old monastery to the north."
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["world_id"] == world_id
    assert payload["committed"] == []
    assert payload["proposals"], "a place claim should produce a proposal"
    assert payload["proposals"][0]["status"] == "pending"
    store = routes_engine._get_store()
    try:
        world = store.get_world(world_id)
    finally:
        store.close()
    ids = [location.id for location in world.locations]
    assert ids == ["square", "chapel"], "a dry run must not touch the world"


def test_commit_adds_the_place_and_records_it(client, world_id):
    from app.api import routes_engine

    response = _propose(
        client,
        world_id,
        text="There is an old monastery to the north.",
        commit=True,
        auto_commit=0.3,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["committed"], payload
    store = routes_engine._get_store()
    try:
        world = store.get_world(world_id)
        rows = store.list_proposals(world_id=world_id)
    finally:
        store.close()
    assert len(world.locations) == 3
    assert rows and rows[0]["status"] == "auto_committed"
    assert rows[0]["world_id"] == world_id


def test_a_redeclared_place_is_rejected_with_a_reason(client, world_id):
    response = _propose(
        client,
        world_id,
        statements=[
            {
                "text": "Old Chapel",
                "kind": "location",
                "confidence": 0.9,
                "entities": ["Old Chapel"],
            }
        ],
        commit=True,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["conflicts"], payload
    assert "already defines" in payload["conflicts"][0]
    assert payload["committed"] == []


def test_a_rumour_never_becomes_canon(client, world_id):
    response = _propose(
        client,
        world_id,
        text="I heard the monks kept a locked vault in the cellar.",
        commit=True,
        auto_commit=0.0,
    )
    assert response.status_code == 200
    payload = response.json()
    kinds = [item["kind"] for item in payload["proposals"]]
    assert "rumour" in kinds
    assert payload["committed"] == []
    assert all(item["canonical"] is False for item in payload["proposals"])


def test_unknown_world_is_a_404(client):
    response = _propose(client, "nope", text="There is a tower.")
    assert response.status_code == 404


def test_a_pending_proposal_can_be_approved(client, world_id):
    from app.api import routes_engine

    proposed = _propose(
        client, world_id, text="There is an old monastery to the north."
    ).json()
    proposal_id = proposed["proposals"][0]["id"]
    assert proposed["proposals"][0]["status"] == "pending"
    response = client.post(
        "/api/v1/worlds/{0}/evolution/proposals/{1}/approve".format(world_id, proposal_id)
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["committed"] == [proposal_id]
    store = routes_engine._get_store()
    try:
        row = store.get_proposal(proposal_id)
    finally:
        store.close()
    assert row["status"] == "committed"


def test_approving_twice_is_a_conflict(client, world_id):
    proposed = _propose(
        client, world_id, text="There is an old monastery to the north."
    ).json()
    proposal_id = proposed["proposals"][0]["id"]
    url = "/api/v1/worlds/{0}/evolution/proposals/{1}/approve".format(world_id, proposal_id)
    assert client.post(url).status_code == 200
    assert client.post(url).status_code == 409


def test_a_pending_proposal_can_be_rejected_with_a_reason(client, world_id):
    from app.api import routes_engine

    proposed = _propose(
        client, world_id, text="There is an old monastery to the north."
    ).json()
    proposal_id = proposed["proposals"][0]["id"]
    response = client.post(
        "/api/v1/worlds/{0}/evolution/proposals/{1}/reject?reason=not+in+scope".format(
            world_id, proposal_id
        )
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    store = routes_engine._get_store()
    try:
        row = store.get_proposal(proposal_id)
    finally:
        store.close()
    assert row["status"] == "rejected"
    assert "scope" in row["error"]


def test_proposals_are_listed_and_filterable(client, world_id):
    _propose(client, world_id, text="There is an old monastery to the north.")
    response = client.get(
        "/api/v1/worlds/{0}/evolution/proposals".format(world_id)
    )
    assert response.status_code == 200
    rows = response.json()
    assert rows and rows[0]["statement"].startswith("There is")
    assert rows[0]["canonical"] is True
    filtered = client.get(
        "/api/v1/worlds/{0}/evolution/proposals?status=rejected".format(world_id)
    ).json()
    assert filtered == []


def test_a_claim_with_no_op_is_never_reported_as_committed(client, world_id):
    """Nothing wrote an op for this claim, so nothing may call it committed."""
    from app.api import routes_engine
    from app.services import world_evolution as evo

    store = routes_engine._get_store()
    try:
        world = store.get_world(world_id)
    finally:
        store.close()
    service = evo.WorldEvolutionService(store=none_store(), persist=lambda w: None)
    proposal = evo.EvolutionProposal(
        id="evo_noop",
        kind="world_canon",
        statement="The moon is made of salt.",
        confidence=0.99,
        world_id=world_id,
    )
    assert proposal.payload.get("op") is None
    assert service._eligible(proposal) is True

    report = evo.EvolutionReport(world_id=world_id)
    report.proposals.append(proposal)
    report.pending.append(proposal.id)
    report = service.commit(report, world, draft=world.model_copy(deep=True), applied=[])
    assert report.committed == []
    assert proposal.status == evo.STATUS_PENDING


class none_store:
    """A recorder that keeps nothing: this test only exercises commit()."""

    def record_proposal(self, proposal):
        return {}


def test_a_claim_the_world_already_has_is_never_called_committed(client, world_id):
    """The conflict check refuses it, so nothing may be reported as committed."""
    from app.api import routes_engine
    from app.services import world_evolution as evo

    payload = _propose(
        client,
        world_id,
        statements=[{
            """text""": """The Old Chapel still stands at the crossroads.""",
            """kind""": """location""",
            """confidence""": 0.95,
            """entities""": ["""Old Chapel"""],
        }],
        commit=True,
        auto_commit=0.0,
    ).json()
    assert payload["""committed"""] == []
    assert payload["""rejected"""]
    store = routes_engine._get_store()
    try:
        rows = store.list_proposals(world_id=world_id, limit=5)
    finally:
        store.close()
    assert rows[0]["""status"""] == evo.STATUS_REJECTED
