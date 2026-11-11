"""GET /api/v1/local-model reports NInfer reachability without mutating state."""
import pytest

from app.api import routes_engine
from app.config import settings


@pytest.mark.asyncio
async def test_local_model_status_reports_unreachable_server(monkeypatch):
    monkeypatch.setattr(settings, "local_llm_base_url", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(settings, "local_llm_api_key", "")
    status = await routes_engine.local_model_status()
    assert status.configured is True
    assert status.reachable is False
    assert status.detail  # readable failure, not an exception


@pytest.mark.asyncio
async def test_local_model_status_handles_missing_configuration(monkeypatch):
    monkeypatch.setattr(settings, "local_llm_base_url", "")
    status = await routes_engine.local_model_status()
    assert status.configured is False
    assert status.reachable is False
    assert "LOCAL_LLM_BASE_URL" in status.detail
