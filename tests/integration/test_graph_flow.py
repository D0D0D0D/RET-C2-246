# RET-C2-246 — Integration Tests: full graph invoke through the 5-node backbone

import json

import pytest

from src.graph.graph import RETInStoreSafetyAlertAgent
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.schemas.agent_status import AgentStatus


@pytest.fixture
def agent(fake_llm):
    # Production wiring: config["llm"] -> register_nodes() -> MainNode.
    # 🔴 This used to rewrite the private a._nodes["main"]._llm attribute, which
    # kept the suite green while the real path (config -> graph -> node) was not
    # connected at all. Going through config is what proves the wiring.
    a = RETInStoreSafetyAlertAgent(config={"llm": fake_llm})
    a.compile()
    return a


def _ctx(trust=TrustLevel.VERIFIED_EXTERNAL):
    return InvocationContext(session_id="it", caller_trust_level=trust, caller_id="tester")


class TestGraphFlow:
    def test_full_backbone_success(self, agent, sample_payload):
        out = agent.invoke(sample_payload, ctx=_ctx())
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["node_history"] == [
            "InitializeNode",
            "PreProcessNode",
            "MainNode",
            "PostProcessNode",
            "FinalizeNode",
        ]
        body = out["output"]
        assert body["ok"] is True
        assert body["alert"]
        assert body["incident_log"]["incident_count"] == 2

    def test_trend_surfaced_with_history(self, agent):
        payload = json.dumps(
            {
                "incidents": [{"location": "A", "type": "wet floor"}],
                "historical_incidents": [{"location": "A"}],
            }
        )
        out = agent.invoke(payload, ctx=_ctx())
        assert out["output"]["trend"] != ""

    def test_invalid_input_routes_to_error(self, agent):
        out = agent.invoke(json.dumps({"incidents": []}), ctx=_ctx())
        assert out["status"] == AgentStatus.ERROR.value

    def test_s1_trust_denied_for_anonymous(self, agent, sample_payload):
        """S-1: ANONYMOUS caller is denied at the first privileged node."""
        out = agent.invoke(sample_payload, ctx=_ctx(TrustLevel.ANONYMOUS))
        assert out["status"] == AgentStatus.ERROR.value

    def test_no_llm_in_config_fails_closed(self, sample_payload):
        """Default build (no config["llm"]) errors — it must not fabricate content.

        This is the develop behaviour preserved: a deploy with no provider is a
        provisioning mistake, not a transient fault.
        """
        a = RETInStoreSafetyAlertAgent()
        a.compile()
        out = a.invoke(sample_payload, ctx=_ctx())
        assert out["status"] == AgentStatus.ERROR.value
        assert out["output"] is None

    def test_provider_failure_degrades_to_deterministic_alert(self, sample_payload):
        """A reachable-but-failing provider must not silence a detected hazard."""

        class _Boom:
            def generate_safety_content(self, **kwargs):
                raise TimeoutError("provider timed out: <echoed request body>")

        a = RETInStoreSafetyAlertAgent(config={"llm": _Boom()})
        a.compile()
        out = a.invoke(sample_payload, ctx=_ctx())
        assert out["status"] == AgentStatus.SUCCESS.value
        body = out["output"]
        assert body["degradation_reason"] == "llm_failed:TimeoutError"
        # The deterministic detection result still reaches the store staff.
        assert "fire_hazard" in body["alert"]
        assert "Aisle 3" in body["alert"]
        # The provider's message never reaches the caller.
        assert "echoed request body" not in str(out)
