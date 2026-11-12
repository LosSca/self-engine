"""Proxy-env normalisation: the one bug that silently kills web search."""
from __future__ import annotations

from app.services import web_search as ws


def test_proxy_env_normalisation_drops_ipv6_literals(monkeypatch):
    """httpx dies on NO_PROXY entries like "::1" / "[::1]" (InvalidURL)."""
    monkeypatch.setenv("NO_PROXY", "172.29.79.110,localhost,127.0.0.1,::1,[::1]")
    saved = ws._clean_proxy_env()
    try:
        import os

        value = os.environ["NO_PROXY"]
        assert "::1" not in value
        assert "127.0.0.1" in value
        assert "localhost" in value
    finally:
        import os

        for key, original in saved.items():
            os.environ[key] = original
        # The original value comes back untouched once the call is over.
        assert os.environ["NO_PROXY"] == "172.29.79.110,localhost,127.0.0.1,::1,[::1]"
