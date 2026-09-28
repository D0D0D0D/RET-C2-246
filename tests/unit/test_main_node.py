# RET-C2-246 — Unit Tests: main node (deterministic scoring + LLM seam + OBS-1)

import inspect

import pytest

from src.nodes.main_node import MainNode
from src.services.llm_client import UnconfiguredLLMClient
from framework.schemas.agent_status import AgentStatus


def _classified():
    return [
        {"id": "i1", "location": "Entrance", "category": "slip_zone", "people_affected": 6},
        {"id": "i2", "location": "Aisle 3", "category": "fire_hazard", "injury": True},
    ]


class TestMainNode:
    def test_success_path(self, fake_llm):
        node = MainNode(llm_client=fake_llm)
        res = node.execute({"classified_incidents": _classified(), "locale": "ja"})
        assert res["status"] == AgentStatus.SUCCESS
        assert len(res["severity_scores"]) == 2
        assert res["safety_alert"]  # non-empty
        assert res["reporting_advisory"]
        assert fake_llm.calls and fake_llm.calls[0]["locale"] == "ja"

    def test_trend_empty_without_history(self, fake_llm):
        res = MainNode(llm_client=fake_llm).execute(
            {"classified_incidents": _classified(), "historical_incidents": []}
        )
        assert res["pattern_trend"] == ""

    def test_trend_present_with_history(self, fake_llm):
        res = MainNode(llm_client=fake_llm).execute(
            {"classified_incidents": _classified(),
             "historical_incidents": [{"location": "Entrance"}]}
        )
        assert res["pattern_trend"] != ""

    def test_obs1_upstream_error_passthrough(self, fake_llm):
        """OBS-1: main must pass through without overwriting an upstream error."""
        res = MainNode(llm_client=fake_llm).execute({"status": AgentStatus.ERROR})
        assert res == {}
        assert fake_llm.calls == []  # LLM not called on the error path

    def test_unconfigured_llm_fails_closed(self):
        """No injected client -> fail loudly, never fabricate safety content."""
        node = MainNode()  # default UnconfiguredLLMClient
        assert isinstance(node._llm, UnconfiguredLLMClient)
        with pytest.raises(NotImplementedError):
            node.execute({"classified_incidents": _classified()})

    def test_execute_method_signature(self):
        """node contract: execute(self, state); no _invoke_impl override."""
        sig = inspect.signature(MainNode.execute)
        params = list(sig.parameters.keys())
        assert params[1] == "state"
        assert "_invoke_impl" not in MainNode.__dict__
