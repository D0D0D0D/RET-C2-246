"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.

from __future__ import annotations

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """RET-C2-246 in-store safety alert agent state.

    All shared fields (user_input, status, session_id, node_history,
    error_log, result, validated_input, input_context, hitl_*, etc.) are
    inherited from AgentState. Only domain-specific fields are added below.

    All fields are flat, JSON-serializable (str / int / list / dict) — no
    Pydantic, no dataclass, no credentials (S-1/PB-2 state safety).
    """

    # --- input (caller-provided) ---
    # Prior incidents passed in by the caller for trend generation. The agent
    # is stateless and does NOT accumulate these itself (accumulation is the
    # upstream system's responsibility — see docs/02_design.md).
    historical_incidents: list  # list[dict], optional
    locale: str  # output language for generated content (default "ja")

    # --- pre_process outputs ---
    normalized_incidents: list  # list[dict]: normalized incident records
    classified_incidents: list  # list[dict]: incidents with anomaly category

    # --- main outputs ---
    severity_scores: list  # list[dict]: deterministic severity per incident
    safety_alert: str  # LLM-generated on-site safety alert (in locale)
    reporting_advisory: str  # non-binding reporting advisory (in locale)
    pattern_trend: str  # location-wise risk trend (empty if no history)
    # "" on the normal path. Set when the LLM was injected but its answer was
    # unusable and the deterministic detection result was rendered instead.
    # Fixed vocabulary + exception type name only — never provider text.
    degradation_reason: str

    # --- post_process output ---
    incident_log: dict  # PII-free aggregated incident log
