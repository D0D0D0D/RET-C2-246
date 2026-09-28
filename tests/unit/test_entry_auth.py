"""Entry-adapter authentication tests for src/api/server.py.

Verifies the fail-closed entry auth on /invoke:
  - no verified trust and no INVOKE_AUTH_TOKEN promotion → 401
  - correct Bearer token → ANONYMOUS promoted to VERIFIED_EXTERNAL → 200
  - wrong Bearer token → 401
  - /health requires no auth → 200

Skipped when the framework wheel (or fastapi TestClient) is unavailable, so the
file collects cleanly in a bare CI/dev environment.
"""

from __future__ import annotations

import importlib

import pytest

framework = pytest.importorskip("framework")  # noqa: F841
fastapi_testclient = pytest.importorskip("fastapi.testclient")

TestClient = fastapi_testclient.TestClient

TOKEN = "mock-invoke-token-for-testing"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", TOKEN)
    server = importlib.import_module("src.api.server")
    importlib.reload(server)

    # Neutralize the real agent so tests exercise only the auth boundary.
    class _StubAgent:
        _secrets_provider = None

        def invoke(self, _input, ctx=None):
            return {"status": "success", "trust": str(ctx.caller_trust_level)}

    monkeypatch.setattr(server, "agent", _StubAgent())
    import contextlib

    monkeypatch.setattr(server, "bound_secrets", lambda _p: contextlib.nullcontext())
    return TestClient(server.app)


def test_invoke_unauthenticated_is_rejected(client):
    resp = client.post("/invoke", json={"input": "x", "session_id": "s1"})
    assert resp.status_code == 401


def test_invoke_wrong_token_is_rejected(client):
    resp = client.post(
        "/invoke",
        json={"input": "x", "session_id": "s1"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


def test_invoke_valid_token_is_accepted(client):
    resp = client.post(
        "/invoke",
        json={"input": "x", "session_id": "s1"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 200


def test_health_needs_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200


# ── standalone adapter wiring ──────────────────────────────────────────────
#
# 🔴 The tests above stub out `server.agent`, so nothing observed how the real
# agent was constructed. Reverting `RETInStoreSafetyAlertAgent(config=_config)`
# back to a no-arg build left the whole suite green (mutation M17, 2026-08-28)
# even though that line is the only route by which config/config.yaml — and
# therefore the config["llm"] injection seam — reaches the graph on this path.


def test_standalone_adapter_builds_the_agent_from_config_yaml():
    """config/config.yaml must reach the graph on the standalone path."""
    server = importlib.import_module("src.api.server")
    importlib.reload(server)
    # Literal values from config/config.yaml — not read back from the file, so
    # a config that silently stops being loaded is visible here.
    assert server.agent.config.get("max_retry") == 3
    assert server.agent.config.get("locale") == "ja"


def test_standalone_adapter_leaves_the_llm_seam_open():
    """This adapter supplies no provider; the agent must fail closed, not fabricate."""
    from src.services.llm_client import UnconfiguredLLMClient

    server = importlib.import_module("src.api.server")
    importlib.reload(server)
    assert server.agent.config.get("llm") is None
    assert isinstance(server.agent._nodes["main"]._llm, UnconfiguredLLMClient)
