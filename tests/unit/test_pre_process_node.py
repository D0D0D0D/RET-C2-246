# RET-C2-246 — Unit Tests: pre_process node (ingest + classify + S-2 gate)

import json

from src.nodes.pre_process_node import PreProcessNode, MAX_INPUT_CHARS
from framework.schemas.agent_status import AgentStatus


class TestPreProcessExecute:
    def setup_method(self):
        self.node = PreProcessNode()

    def _payload(self, **extra):
        data = {
            "locale": "ja",
            "incidents": [
                {"id": "i1", "location": "Entrance", "type": "wet floor slip"},
                {"id": "i2", "location": "Aisle 3", "type": "fire hazard"},
            ],
        }
        data.update(extra)
        return json.dumps(data)

    def test_valid_payload_success(self):
        res = self.node.execute({"user_input": self._payload(), "input_context": {}})
        assert res["status"] == AgentStatus.SUCCESS
        assert len(res["normalized_incidents"]) == 2
        assert {c["category"] for c in res["classified_incidents"]} == {
            "slip_zone",
            "fire_hazard",
        }
        assert res["locale"] == "ja"

    def test_locale_defaults_to_ja(self):
        res = self.node.execute(
            {"user_input": json.dumps({"incidents": [{"location": "A", "type": "slip"}]}),
             "input_context": {}}
        )
        assert res["locale"] == "ja"

    def test_locale_from_input_context(self):
        res = self.node.execute(
            {"user_input": json.dumps({"incidents": [{"location": "A", "type": "slip"}]}),
             "input_context": {"locale": "en"}}
        )
        assert res["locale"] == "en"

    def test_empty_input_error(self):
        res = self.node.execute({"user_input": "", "input_context": {}})
        assert res["status"] == AgentStatus.ERROR

    def test_invalid_json_error(self):
        res = self.node.execute({"user_input": "not-json{", "input_context": {}})
        assert res["status"] == AgentStatus.ERROR

    def test_no_incidents_error(self):
        res = self.node.execute(
            {"user_input": json.dumps({"incidents": []}), "input_context": {}}
        )
        assert res["status"] == AgentStatus.ERROR

    def test_missing_required_field_error(self):
        res = self.node.execute(
            {"user_input": json.dumps({"incidents": [{"location": "A"}]}),
             "input_context": {}}
        )
        assert res["status"] == AgentStatus.ERROR
        assert "location" in res["error_log"][0] or "type" in res["error_log"][0]

    def test_historical_incidents_passthrough(self):
        res = self.node.execute(
            {"user_input": self._payload(historical_incidents=[{"location": "Entrance"}]),
             "input_context": {}}
        )
        assert res["historical_incidents"] == [{"location": "Entrance"}]


class TestPreProcessInputGate:
    def setup_method(self):
        self.node = PreProcessNode()

    def test_oversized_input_rejected(self):
        big = "x" * (MAX_INPUT_CHARS + 1)
        out = self.node._extra_security_gate_input({"user_input": big})
        assert out["status"] == AgentStatus.ERROR

    def test_normal_input_passes_gate(self):
        out = self.node._extra_security_gate_input({"user_input": "small"})
        assert out.get("status") != AgentStatus.ERROR
