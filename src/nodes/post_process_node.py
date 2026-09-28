"""AgentCore Platform v1.0 — RET-C2-246 post_process node.

IncidentLogOutput + S-3 OutputGate:
  - build a PII-free, aggregated incident log (counts only, no raw records)
  - assemble the final result dict
  - S-3: scrub residual PII (email / phone / customer-id patterns) from all
    generated string output via _extra_security_gate_output()

S-3 note: the framework @final _security_gate_output() runs the default
credential scan first, then delegates to _extra_security_gate_output() — we
extend the latter only (never override the @final gate).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event

# PII patterns scrubbed from generated output before it leaves the agent.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\-\s]{8,}\d)(?!\d)")
_CUSTOMER_ID = re.compile(r"(?i)\b(?:customer[_-]?id|顧客(?:番号|ID))\s*[:：]?\s*\S+")
_REDACTION = "[REDACTED]"


def _scrub(value: str) -> str:
    value = _EMAIL.sub(_REDACTION, value)
    value = _CUSTOMER_ID.sub(_REDACTION, value)
    value = _PHONE.sub(_REDACTION, value)
    return value


def _scrub_deep(obj: Any) -> Any:
    if isinstance(obj, str):
        return _scrub(obj)
    if isinstance(obj, dict):
        # Scrub keys too — incident_log aggregates by attacker-controlled
        # `location` strings used as dict keys, which would otherwise leak PII.
        return {
            (_scrub(k) if isinstance(k, str) else k): _scrub_deep(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub_deep(v) for v in obj]
    return obj


class PostProcessNode(FunctionNode):
    """Aggregate a PII-free incident log and assemble the final result."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        # Defensive upstream-error pass-through (route normally skips this node
        # on error, but never overwrite an error status if reached).
        if state.get("status") == AgentStatus.ERROR:
            return {}

        classified = state.get("classified_incidents", []) or []
        severity_scores = state.get("severity_scores", []) or []
        locale = state.get("locale", "ja")

        # Aggregate-only log (no raw incident text / PII).
        by_location: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for inc in classified:
            by_location[inc.get("location", "unknown")] = (
                by_location.get(inc.get("location", "unknown"), 0) + 1
            )
            by_category[inc.get("category", "other")] = (
                by_category.get(inc.get("category", "other"), 0) + 1
            )
        severity_summary: dict[str, int] = {}
        for s in severity_scores:
            severity_summary[s.get("severity", "low")] = (
                severity_summary.get(s.get("severity", "low"), 0) + 1
            )

        incident_log = {
            "incident_count": len(classified),
            "by_location": by_location,
            "by_category": by_category,
            "severity_summary": severity_summary,
            "generated_locale": locale,
        }

        result = {
            "alert": state.get("safety_alert", ""),
            "advisory": state.get("reporting_advisory", ""),
            "trend": state.get("pattern_trend", ""),
            "incident_log": incident_log,
            # "" on the normal path. Non-empty means the alert/advisory text is
            # the deterministic rendering, not model-written prose.
            "degradation_reason": state.get("degradation_reason", ""),
            "ok": True,
        }

        emit_trace_event(
            "incident_log_built",
            {"incident_count": len(classified)},
            state,
        )

        return {
            "incident_log": incident_log,
            "result": result,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 domain extension: scrub residual PII from all generated output."""
        return _scrub_deep(result)
