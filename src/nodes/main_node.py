"""AgentCore Platform v1.0 — RET-C2-246 main node.

SeverityScore + AlertGenerate + ReportingAdvisory + PatternTrend:
  - deterministic severity scoring (reproducible, no LLM)
  - LLM-generated safety alert / reporting advisory / location trend, in the
    caller's locale, via a client injected through ``config["llm"]``
    (keys stay out of State)

The model never decides *whether* something is an anomaly or *how severe* it is
— ``severity_service`` settles both deterministically before the model is
called. The model only writes the prose for facts already established.

Degradation policy (2026-08-28):
  - **no client injected** -> fail (``status: error``). A deploy with no LLM is
    a provisioning mistake; degrading it silently would leave a safety agent
    permanently rendering machine text with nobody noticing.
  - **client injected but the call fails / is unusable** -> render the
    deterministic detection result and continue (``status: success``) with
    ``degradation_reason`` set. A provider outage must not turn a detected fire
    hazard into no alert at all.

OBS-1 (review): pre_process -> main is an unconditional edge, so main
runs even when an upstream node set status=ERROR. main MUST pass through early
without overwriting the error.
"""

from __future__ import annotations

import copy
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event
from src.services.llm_client import (
    KNOWN_UNUSABLE_REASONS,
    build_high_severity_roster,
    REASON_HIGH_SEVERITY_OMITTED,
    REASON_UNSPECIFIED,
    LLMClient,
    SafetyContentUnusableError,
    UnconfiguredLLMClient,
    build_degraded_safety_content,
    finalize_safety_content,
    resolve_llm_client,
    sanitize_model_name,
    verify_high_severity_coverage,
)
from src.services.severity_service import score_incidents


class MainNode(FunctionNode):
    """Deterministic severity scoring + LLM safety-content generation."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        super().__init__()
        # The graph passes config["llm"] here. resolve_llm_client() adapts a
        # BaseLLM to this agent's protocol, passes a domain client through
        # unchanged, and falls back to the fail-closed sentinel when nothing
        # usable was injected. Keys are NEVER read here or stored in State.
        self._llm: LLMClient = resolve_llm_client(llm_client)

    def execute(self, state: dict) -> dict:
        # OBS-1: upstream error pass-through (pre_process -> main is unconditional).
        if state.get("status") == AgentStatus.ERROR:
            return {}

        classified = state.get("classified_incidents", []) or []
        historical = state.get("historical_incidents", []) or []
        locale = state.get("locale", "ja")

        # Deterministic, reproducible severity scoring (no LLM).
        severity_scores = score_incidents(classified)
        emit_trace_event(
            "severity_scored",
            {
                "count": len(severity_scores),
                "max_score": severity_scores[0]["score"] if severity_scores else 0,
            },
            state,
        )

        # 🔴 The unconfigured case is decided by TYPE, before the call — never by
        # catching NotImplementedError from an injected client. The framework
        # writes str(exc) AND the traceback of any escaping exception into
        # state["error_log"] (framework/nodes/base_node.py), so letting a
        # provider's exception escape would put its message — which can echo the
        # caller's request body — straight into State.
        if isinstance(self._llm, UnconfiguredLLMClient):
            raise NotImplementedError(
                "No LLM is configured for RET-C2-246. Inject one via " "config={'llm': <BaseLLM>} before invoking."
            )

        # LLM generation of natural-language safety content (in locale).
        degradation_reason = ""
        model = ""
        try:
            # 🔴 Hand the client COPIES. These lists are the authoritative
            # deterministic record the preservation gate checks against, and a
            # duck-typed domain client (an explicitly supported injection shape)
            # receives them by reference. One that called
            # `severity_scores.clear()` and returned "All clear" produced a clean
            # success with no degradation — the gate had nothing left to check.
            # Measured 2026-08-29.
            content = self._llm.generate_safety_content(
                classified_incidents=copy.deepcopy(classified),
                severity_scores=copy.deepcopy(severity_scores),
                historical_incidents=copy.deepcopy(historical),
                locale=locale,
            )
            model = sanitize_model_name(content.get("model"))
        except SafetyContentUnusableError as exc:
            # The code is attacker-reachable on a directly injected client, so it
            # is admitted only when it is one of ours.
            code = exc.code if exc.code in KNOWN_UNUSABLE_REASONS else REASON_UNSPECIFIED
            degradation_reason = f"llm_unusable_response:{code}"
            content = build_degraded_safety_content(severity_scores=severity_scores)
        except Exception as exc:  # noqa: BLE001 — a provider fault must not silence the alert
            # Exception TYPE only. str(exc) can echo the request body or carry
            # credentials, and error_log/State are persisted.
            degradation_reason = f"llm_failed:{type(exc).__name__}"
            content = build_degraded_safety_content(severity_scores=severity_scores)

        # Deterministic output guards, applied to every client's content.
        content = finalize_safety_content(content, has_history=bool(historical))

        # ── S-3 preservation variant ────────────────────────────────────────
        # Two mechanisms, because the text match alone cannot carry the promise:
        #   (a) SIGNAL — check the MODEL'S prose. If a high-severity incident it
        #       should have named is absent, degrade to the deterministic
        #       rendering. Done here, not in the gate hook: the hook cannot
        #       rebuild content, and a hook that rewrote the alert would leave
        #       the audit event describing text that was never produced.
        #   (b) GUARANTEE — append the deterministic roster of every
        #       high-severity incident. Token matching cannot decide a location
        #       with no identifier anchor, and cannot tell "at Aisle 7" from
        #       "all clear at Aisle 7"; appending the facts is unconditional and
        #       language-independent.
        # (a) runs before (b), or the roster we just appended would satisfy the
        # check and make it vacuous.
        prose_missing, prose_unverifiable = verify_high_severity_coverage(content.get("alert", ""), severity_scores)
        if prose_missing and not degradation_reason:
            # Fall back to the deterministic rendering rather than failing: an
            # alert that still names every hazard beats no alert at all.
            degradation_reason = REASON_HIGH_SEVERITY_OMITTED
            content = finalize_safety_content(
                build_degraded_safety_content(severity_scores=severity_scores),
                has_history=bool(historical),
            )

        if not degradation_reason:
            # Not on the degraded path: build_degraded_safety_content() already
            # lists every scored incident verbatim, so appending here too would
            # print the same hazard twice and read as two separate findings.
            roster = build_high_severity_roster(severity_scores)
            if roster:
                content = dict(content)
                content["alert"] = f"{content.get('alert', '')}\n{roster}".strip()

        emit_trace_event(
            "safety_content_generated",
            {
                "locale": locale,
                "has_trend": bool(content.get("trend")),
                # The model that actually answered, from the canonical response.
                "model": model,
                "degradation_reason": degradation_reason,
                # Counts only — a location is caller-supplied free text and must
                # not be copied into the audit log.
                # How the MODEL's own prose scored, before the deterministic
                # roster was appended — that is the informative signal.
                "high_severity_omitted_by_model": len(prose_missing),
                "high_severity_unverifiable_in_prose": len(prose_unverifiable),
            },
            state,
        )

        return {
            "severity_scores": severity_scores,
            "safety_alert": content.get("alert", ""),
            "reporting_advisory": content.get("advisory", ""),
            "pattern_trend": content.get("trend", ""),
            "degradation_reason": degradation_reason,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 preservation backstop — verification only, never transformation.

        execute() has already degraded any content that dropped a high-severity
        incident, so this should never fire. It exists so that a bug in that path
        cannot ship an alert with a hazard silently missing.

        Fail-closed on absence: a dropped field is an anomaly, not a reason to
        skip the check. (`.get(key, [])` here would make a missing field read as
        "nothing to verify" — the exact fail-open shape this project has been
        bitten by before.)
        """
        if not result:
            # OBS-1: execute() returns {} to pass an upstream error through
            # untouched. Nothing was generated, so there is nothing to preserve.
            return result
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        severity_scores = result.get("severity_scores")
        alert = result.get("safety_alert")
        if severity_scores is None or alert is None:
            raise RuntimeError(
                "S-3 preservation gate: severity_scores / safety_alert missing "
                "from a successful MainNode result — cannot verify that "
                "high-severity incidents survived into the alert."
            )
        missing, _unverifiable = verify_high_severity_coverage(alert, severity_scores)
        if missing:
            # Count only — locations are caller-supplied text.
            raise RuntimeError(
                f"S-3 preservation gate: {len(missing)} high-severity incident(s) "
                "are absent from the generated alert. Output blocked."
            )
        return result
