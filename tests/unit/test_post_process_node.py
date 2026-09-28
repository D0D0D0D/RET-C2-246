# RET-C2-246 — Unit Tests: post_process node (incident log + S-3 PII scrub)

from src.nodes.post_process_node import PostProcessNode
from framework.schemas.agent_status import AgentStatus


def _state():
    return {
        "classified_incidents": [
            {"id": "i1", "location": "Entrance", "category": "slip_zone"},
            {"id": "i2", "location": "Aisle 3", "category": "fire_hazard"},
        ],
        "severity_scores": [
            {"location": "Aisle 3", "score": 10, "severity": "high"},
            {"location": "Entrance", "score": 3, "severity": "low"},
        ],
        "safety_alert": "alert text",
        "reporting_advisory": "advisory text",
        "pattern_trend": "",
        "locale": "ja",
    }


class TestPostProcessExecute:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_builds_incident_log_and_result(self):
        res = self.node.execute(_state())
        assert res["status"] == AgentStatus.SUCCESS
        log = res["incident_log"]
        assert log["incident_count"] == 2
        assert log["by_location"] == {"Entrance": 1, "Aisle 3": 1}
        assert log["by_category"] == {"slip_zone": 1, "fire_hazard": 1}
        assert log["severity_summary"] == {"high": 1, "low": 1}
        assert res["result"]["ok"] is True
        assert res["result"]["alert"] == "alert text"

    def test_incident_log_has_no_raw_pii_fields(self):
        """Aggregate-only log must not carry raw descriptions / reporter notes."""
        res = self.node.execute(_state())
        log_str = str(res["incident_log"])
        assert "description" not in log_str and "reporter_note" not in log_str

    def test_obs1_error_passthrough(self):
        assert self.node.execute({"status": AgentStatus.ERROR}) == {}


class TestPostProcessOutputGate:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_scrubs_email_phone_customer_id(self):
        result = {
            "alert": "reach john.doe@example.com or 090-1234-5678",
            "advisory": "customer_id: CUST-99",
            "incident_log": {"note": "contact user@example.com"},
        }
        out = self.node._extra_security_gate_output(result)
        assert "@example.com" not in out["alert"]
        assert "090-1234-5678" not in out["alert"]
        assert "CUST-99" not in out["advisory"]
        assert "user@example.com" not in out["incident_log"]["note"]

    def test_scrubs_pii_in_dict_keys(self):
        """PII used as a dict key (e.g. an email as a 'location') must be scrubbed."""
        result = {"incident_log": {"by_location": {"susan+alerts@example.com": 2}}}
        out = self.node._extra_security_gate_output(result)
        assert "@example.com" not in str(out)

    def test_clean_output_unchanged(self):
        result = {"alert": "Mind the wet floor near the entrance.", "ok": True}
        out = self.node._extra_security_gate_output(result)
        assert out["alert"] == "Mind the wet floor near the entrance."
        assert out["ok"] is True
