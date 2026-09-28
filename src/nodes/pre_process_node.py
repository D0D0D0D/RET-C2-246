"""AgentCore Platform v1.0 — RET-C2-246 pre_process node.

IncidentDataIngest + AnomalyClassify:
  - parse the caller-provided structured incident payload (JSON string)
  - validate required fields (location, type) — S-2 domain input gate
  - normalize records and attach a canonical anomaly category

Node contract:
  - Extend FunctionNode; implement execute(state) -> dict
  - Return ONLY the fields this node changes (never the full state)
  - Return AgentStatus enum constants — never plain strings
  - S-1 trust: required_trust_level enforced by BaseNode.__call__()
  - S-2: extend via _extra_security_gate_input() only (never override @final gate)
  - S-4: emit_trace_event(event, payload, state) inside execute()
"""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event
from src.services.severity_service import classify_incidents

# Reject payloads larger than this many characters (S-2 input size guard).
MAX_INPUT_CHARS = 100_000


class PreProcessNode(FunctionNode):
    """Ingest, validate and classify the structured incident payload."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        raw = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not raw or not str(raw).strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "PreProcessNode: user_input is not valid JSON "
                    "(expected an object with an 'incidents' list)"
                ],
            }

        if not isinstance(payload, dict):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: input payload must be a JSON object"],
            }

        incidents = payload.get("incidents", [])
        if not isinstance(incidents, list) or not incidents:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: no incidents provided in payload"],
            }

        # Required-field validation (S-2 domain check that needs the parsed JSON).
        normalized: list[dict] = []
        for idx, inc in enumerate(incidents):
            if not isinstance(inc, dict):
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"PreProcessNode: incident[{idx}] is not an object"],
                }
            location = str(inc.get("location", "")).strip()
            inc_type = str(inc.get("type", "")).strip()
            if not location or not inc_type:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"PreProcessNode: incident[{idx}] missing required field "
                        "'location' or 'type'"
                    ],
                }
            normalized.append(
                {
                    "id": str(inc.get("id", f"inc-{idx}")),
                    "location": location,
                    "type": inc_type,
                    "time": str(inc.get("time", "")),
                    "description": str(inc.get("description", "")),
                    "reporter_note": str(inc.get("reporter_note", "")),
                    "people_affected": inc.get("people_affected", 0),
                    "injury": bool(inc.get("injury", False)),
                }
            )

        classified = classify_incidents(normalized)
        historical = payload.get("historical_incidents", [])
        if not isinstance(historical, list):
            historical = []
        locale = str(payload.get("locale") or input_context.get("locale") or "ja")

        emit_trace_event(
            "incidents_ingested",
            {"count": len(normalized), "has_history": bool(historical)},
            state,
        )
        emit_trace_event(
            "incidents_classified",
            {"categories": sorted({c["category"] for c in classified})},
            state,
        )

        return {
            "validated_input": raw,
            "normalized_incidents": normalized,
            "classified_incidents": classified,
            "historical_incidents": historical,
            "locale": locale,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        """S-2 domain extension: reject oversized payloads (after default PII scan)."""
        raw = state.get("user_input", "")
        if isinstance(raw, str) and len(raw) > MAX_INPUT_CHARS:
            return {
                **state,
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    f"PreProcessNode: input exceeds {MAX_INPUT_CHARS} chars "
                    f"(got {len(raw)})"
                ],
            }
        return state
