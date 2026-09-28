"""RET-C2-246 — LLM injection seam, adapter and degradation policy.

🔴 Expected values are written as literals here on purpose. Deriving them from
the production constants/functions would make the test a self-referential
oracle that agrees with any mutation of them.
"""

from __future__ import annotations

import json

import pytest
from framework.schemas.agent_status import AgentStatus
from shared.services.llm.base_llm import BaseLLM

from src.graph.graph import RETInStoreSafetyAlertAgent
from src.nodes.main_node import MainNode
from src.services.llm_client import (
    BaseLLMSafetyContentAdapter,
    SafetyContentUnusableError,
    UnconfiguredLLMClient,
    build_degraded_safety_content,
    build_safety_content_messages,
    finalize_safety_content,
    resolve_llm_client,
)

_DISCLAIMER_JA = "法的判断ではありません"
_DISCLAIMER_EN = "not a legal determination"


class _StubLLM(BaseLLM):
    """A BaseLLM whose canonical response is fully controlled by the test."""

    def __init__(self, content, model: str = "stub-model-1") -> None:
        self.calls: list = []
        self._content = content
        self._model = model

    def complete(self, messages: list) -> dict:
        self.calls.append(messages)
        return {"content": self._content, "tool_calls": [], "model": self._model}

    def stream(self, messages: list):  # pragma: no cover — unused
        yield ""

    def bind_tools(self, tools: list) -> "_StubLLM":  # pragma: no cover — unused
        return self


class _DelegatingLLM(BaseLLM):
    """A BaseLLM that resolves *any* unknown attribute, like the SDK clients do.

    ``getattr(x, "generate_safety_content")`` succeeds here and returns
    something with the wrong signature — which is why the isinstance(BaseLLM)
    check must run before any duck-typing probe.
    """

    def __init__(self) -> None:
        self.calls: list = []

    def complete(self, messages: list) -> dict:
        self.calls.append(messages)
        return {
            "content": json.dumps({"alert": "a", "advisory": "b", "trend": ""}),
            "model": "delegating-1",
        }

    def stream(self, messages: list):  # pragma: no cover — unused
        yield ""

    def bind_tools(self, tools: list) -> "_DelegatingLLM":  # pragma: no cover
        return self

    def __getattr__(self, name: str):
        def _batch_shaped(*args, **kwargs):
            raise AssertionError(
                f"delegated attribute {name!r} was called — the BaseLLM branch " "must have been taken instead"
            )

        return _batch_shaped


class _DomainClient:
    """A client that already speaks this agent's protocol."""

    def __init__(self) -> None:
        self.calls: list = []

    def generate_safety_content(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"alert": "domain alert", "advisory": "domain advisory", "trend": "t"}


def _scores():
    return [
        {"id": "i2", "location": "Aisle 3", "category": "fire_hazard", "score": 10, "severity": "high"},
        {"id": "i1", "location": "Entrance", "category": "slip_zone", "score": 3, "severity": "medium"},
    ]


class TestResolveLLMClient:
    def test_none_is_unconfigured(self):
        assert isinstance(resolve_llm_client(None), UnconfiguredLLMClient)

    def test_base_llm_is_adapted(self):
        client = resolve_llm_client(_StubLLM('{"alert":"a","advisory":"b","trend":""}'))
        assert isinstance(client, BaseLLMSafetyContentAdapter)

    def test_base_llm_wins_over_getattr_delegation(self):
        """🔴 isinstance(BaseLLM) must be evaluated before duck typing."""
        llm = _DelegatingLLM()
        client = resolve_llm_client(llm)
        assert isinstance(client, BaseLLMSafetyContentAdapter)
        # Calling through must go to complete(), not to the delegated attribute
        # (which raises AssertionError if it is ever reached).
        out = client.generate_safety_content(
            classified_incidents=[], severity_scores=[], historical_incidents=[], locale="ja"
        )
        assert out["alert"] == "a"
        assert len(llm.calls) == 1

    def test_domain_client_passes_through(self):
        domain = _DomainClient()
        assert resolve_llm_client(domain) is domain

    def test_getattr_only_protocol_is_not_accepted_as_domain_client(self):
        """A method that only exists via __getattr__ is not a real implementation."""

        class _Ghost:
            def __getattr__(self, name):
                return lambda **kwargs: {"alert": "ghost", "advisory": "", "trend": ""}

        assert isinstance(resolve_llm_client(_Ghost()), UnconfiguredLLMClient)

    def test_unusable_object_fails_closed(self):
        assert isinstance(resolve_llm_client(object()), UnconfiguredLLMClient)
        assert isinstance(resolve_llm_client("not-a-client"), UnconfiguredLLMClient)

    def test_resolution_is_idempotent(self):
        once = resolve_llm_client(_StubLLM('{"alert":"a","advisory":"b","trend":""}'))
        assert resolve_llm_client(once) is once


class TestAdapterParsing:
    def _adapt(self, content, model="stub-model-1"):
        return BaseLLMSafetyContentAdapter(_StubLLM(content, model)).generate_safety_content(
            classified_incidents=[], severity_scores=[], historical_incidents=[], locale="ja"
        )

    def test_plain_json(self):
        out = self._adapt('{"alert":"A","advisory":"B","trend":"C"}')
        assert out["alert"] == "A"
        assert out["advisory"] == "B"
        assert out["trend"] == "C"

    def test_fenced_json(self):
        out = self._adapt('```json\n{"alert":"A","advisory":"B","trend":""}\n```')
        assert out["alert"] == "A"

    def test_model_comes_from_the_response(self):
        out = self._adapt('{"alert":"A","advisory":"B","trend":""}', model="claude-x-1")
        assert out["model"] == "claude-x-1"

    def test_null_content_is_unusable_never_the_string_none(self):
        """{"content": None} must not become the alert text "None"."""
        with pytest.raises(SafetyContentUnusableError) as exc:
            self._adapt(None)
        assert exc.value.code == "empty_content"

    def test_empty_content_is_unusable(self):
        with pytest.raises(SafetyContentUnusableError) as exc:
            self._adapt("   ")
        assert exc.value.code == "empty_content"

    def test_non_json_is_unusable(self):
        with pytest.raises(SafetyContentUnusableError) as exc:
            self._adapt("I am sorry, I cannot help with that.")
        assert exc.value.code == "not_json"

    def test_broken_json_is_unusable(self):
        with pytest.raises(SafetyContentUnusableError) as exc:
            self._adapt('{"alert": "A", "advisory": ')
        assert exc.value.code == "not_json"

    def test_missing_field_is_unusable(self):
        with pytest.raises(SafetyContentUnusableError) as exc:
            self._adapt('{"alert":"A","trend":""}')
        assert exc.value.code == "schema_mismatch"

    def test_non_string_field_is_unusable(self):
        with pytest.raises(SafetyContentUnusableError) as exc:
            self._adapt('{"alert":"A","advisory":{"x":1},"trend":""}')
        assert exc.value.code == "schema_mismatch"

    def test_blank_alert_is_unusable(self):
        """An empty alert reads as "nothing detected" for a real incident."""
        with pytest.raises(SafetyContentUnusableError) as exc:
            self._adapt('{"alert":"   ","advisory":"B","trend":""}')
        assert exc.value.code == "empty_alert"

    def test_missing_trend_defaults_to_empty(self):
        out = self._adapt('{"alert":"A","advisory":"B"}')
        assert out["trend"] == ""

    def test_invented_keys_are_dropped(self):
        out = self._adapt('{"alert":"A","advisory":"B","trend":"","legal_obligation":"report within 24h"}')
        assert "legal_obligation" not in out

    def test_prompt_carries_the_deterministic_facts(self):
        llm = _StubLLM('{"alert":"A","advisory":"B","trend":""}')
        BaseLLMSafetyContentAdapter(llm).generate_safety_content(
            classified_incidents=[{"id": "i1"}],
            severity_scores=_scores(),
            historical_incidents=[{"location": "Entrance"}],
            locale="vi",
        )
        messages = llm.calls[0]
        assert messages[0]["role"] == "system"
        user_payload = json.loads(messages[1]["content"])
        assert user_payload["locale"] == "vi"
        assert user_payload["severity_scores"] == _scores()
        assert user_payload["historical_incidents"] == [{"location": "Entrance"}]


class TestBuildMessages:
    def test_locale_and_history_are_present(self):
        messages = build_safety_content_messages(
            classified_incidents=[], severity_scores=[], historical_incidents=[], locale="fil"
        )
        assert [m["role"] for m in messages] == ["system", "user"]
        assert json.loads(messages[1]["content"])["locale"] == "fil"


class TestFinalizeGuards:
    def test_trend_forced_empty_without_history(self):
        out = finalize_safety_content(
            {"alert": "A", "advisory": "B", "trend": "a rising trend at the entrance"},
            has_history=False,
        )
        assert out["trend"] == ""

    def test_trend_kept_with_history(self):
        out = finalize_safety_content({"alert": "A", "advisory": "B", "trend": "recurring"}, has_history=True)
        assert out["trend"] == "recurring"

    def test_disclaimer_always_appended(self):
        out = finalize_safety_content(
            {"alert": "A", "advisory": "You must report this to the fire authority."},
            has_history=False,
        )
        assert _DISCLAIMER_JA in out["advisory"]
        assert _DISCLAIMER_EN in out["advisory"]
        # The model's own wording is kept — the disclaimer is added, not a replacement.
        assert "You must report this to the fire authority." in out["advisory"]

    def test_disclaimer_present_even_for_empty_advisory(self):
        out = finalize_safety_content({"alert": "A", "advisory": ""}, has_history=False)
        assert _DISCLAIMER_JA in out["advisory"]

    def test_disclaimer_not_duplicated(self):
        once = finalize_safety_content({"alert": "A", "advisory": "B"}, has_history=False)
        twice = finalize_safety_content(once, has_history=False)
        assert twice["advisory"].count(_DISCLAIMER_JA) == 1


class TestDegradedContent:
    def test_every_scored_incident_is_rendered(self):
        out = build_degraded_safety_content(severity_scores=_scores())
        for token in ("Aisle 3", "fire_hazard", "HIGH", "Entrance", "slip_zone", "MEDIUM"):
            assert token in out["alert"], token

    def test_trend_is_empty(self):
        assert build_degraded_safety_content(severity_scores=_scores())["trend"] == ""

    def test_no_incident_case_is_explicit(self):
        out = build_degraded_safety_content(severity_scores=[])
        assert out["alert"].strip()


class TestMainNodeDegradation:
    def _state(self):
        return {
            "classified_incidents": [
                {"id": "i2", "location": "Aisle 3", "type": "fire hazard near shelf", "injury": True},
                {"id": "i1", "location": "Entrance", "type": "wet floor", "people_affected": 6},
            ],
            "locale": "ja",
        }

    def test_unconfigured_fails_closed(self):
        with pytest.raises(NotImplementedError):
            MainNode().execute(self._state())

    def test_unusable_response_degrades(self):
        node = MainNode(llm_client=_StubLLM("not json at all"))
        res = node.execute(self._state())
        assert res["status"] == AgentStatus.SUCCESS.value
        assert res["degradation_reason"] == "llm_unusable_response:not_json"
        assert "fire_hazard" in res["safety_alert"]
        assert "Aisle 3" in res["safety_alert"]

    def test_blank_alert_degrades(self):
        node = MainNode(llm_client=_StubLLM('{"alert":"","advisory":"B","trend":""}'))
        res = node.execute(self._state())
        assert res["degradation_reason"] == "llm_unusable_response:empty_alert"

    def test_provider_exception_records_type_only(self):
        class _Boom:
            def generate_safety_content(self, **kwargs):
                raise TimeoutError("upstream said: <the caller's raw incident text>")

        res = MainNode(llm_client=_Boom()).execute(self._state())
        assert res["status"] == AgentStatus.SUCCESS.value
        assert res["degradation_reason"] == "llm_failed:TimeoutError"
        # 🔴 canary: the provider's message must not reach State anywhere.
        assert "the caller's raw incident text" not in json.dumps(res, ensure_ascii=False)

    def test_degraded_advisory_still_carries_the_disclaimer(self):
        class _Boom:
            def generate_safety_content(self, **kwargs):
                raise RuntimeError("x")

        res = MainNode(llm_client=_Boom()).execute(self._state())
        assert _DISCLAIMER_JA in res["reporting_advisory"]

    def test_success_path_reports_no_degradation(self, fake_llm):
        res = MainNode(llm_client=fake_llm).execute(self._state())
        assert res["degradation_reason"] == ""
        assert fake_llm.calls


class TestGraphWiring:
    def test_config_llm_reaches_the_node(self):
        llm = _StubLLM('{"alert":"A","advisory":"B","trend":""}')
        agent = RETInStoreSafetyAlertAgent(config={"llm": llm})
        agent.compile()
        assert isinstance(agent._nodes["main"]._llm, BaseLLMSafetyContentAdapter)
        assert agent._nodes["main"]._llm._llm is llm

    def test_default_build_is_unconfigured(self):
        agent = RETInStoreSafetyAlertAgent()
        agent.compile()
        assert isinstance(agent._nodes["main"]._llm, UnconfiguredLLMClient)

    def test_domain_client_in_config_is_not_wrapped(self):
        domain = _DomainClient()
        agent = RETInStoreSafetyAlertAgent(config={"llm": domain})
        agent.compile()
        assert agent._nodes["main"]._llm is domain


# ── Findings from the 2026-08-28 adversarial review ───────────────────────


class TestInjectedClientExceptionsNeverEscape:
    """🔴 The framework writes str(exc) AND the traceback of any escaping
    exception into state["error_log"] (framework/nodes/base_node.py). A provider
    message can echo the caller's request body, so nothing an injected client
    raises may leave execute().
    """

    def _state(self):
        return {"classified_incidents": [{"id": "i", "location": "Aisle 3", "category": "fire_hazard"}]}

    def test_injected_not_implemented_error_degrades_instead_of_escaping(self):
        class _Rude:
            def generate_safety_content(self, **kwargs):
                raise NotImplementedError("provider echo: <the caller's raw payload>")

        res = MainNode(llm_client=_Rude()).execute(self._state())
        assert res["status"] == AgentStatus.SUCCESS.value
        assert res["degradation_reason"] == "llm_failed:NotImplementedError"
        assert "the caller's raw payload" not in json.dumps(res, ensure_ascii=False)

    def test_unconfigured_is_decided_by_type_not_by_exception_class(self):
        """The sentinel still fails closed, and with our own message."""
        with pytest.raises(NotImplementedError) as exc:
            MainNode().execute(self._state())
        assert "RET-C2-246" in str(exc.value)

    def test_unknown_unusable_code_is_not_propagated(self):
        """SafetyContentUnusableError.code is attacker-reachable on a domain client."""

        class _Sneaky:
            def generate_safety_content(self, **kwargs):
                raise SafetyContentUnusableError("RAW_CALLER_TEXT_SMUGGLED_HERE")

        res = MainNode(llm_client=_Sneaky()).execute(self._state())
        assert res["degradation_reason"] == "llm_unusable_response:unspecified"
        assert "SMUGGLED" not in json.dumps(res, ensure_ascii=False)

    def test_known_unusable_code_is_kept(self):
        res = MainNode(llm_client=_StubLLM("not json")).execute(self._state())
        assert res["degradation_reason"] == "llm_unusable_response:not_json"


class TestResolverRejectsUnusableShapes:
    """A bad injection must fail closed at resolve time, not become a
    *successful* degraded run that hides the provisioning mistake."""

    def test_non_callable_attribute_is_rejected(self):
        class _NotCallable:
            generate_safety_content = "oops"

        assert isinstance(resolve_llm_client(_NotCallable()), UnconfiguredLLMClient)

    def test_raising_property_is_rejected(self):
        class _Raising:
            @property
            def generate_safety_content(self):
                raise RuntimeError("boom")

        assert isinstance(resolve_llm_client(_Raising()), UnconfiguredLLMClient)

    def test_class_instead_of_instance_is_rejected(self):
        assert isinstance(resolve_llm_client(_DomainClient), UnconfiguredLLMClient)

    def test_slotted_getattr_object_is_rejected_without_raising(self):
        class _Slotted:
            __slots__ = ()

            def __getattr__(self, name):
                return lambda **kwargs: {}

        assert isinstance(resolve_llm_client(_Slotted()), UnconfiguredLLMClient)

    def test_non_callable_complete_is_rejected(self):
        class _NotCallableComplete:
            complete = 42

        assert isinstance(resolve_llm_client(_NotCallableComplete()), UnconfiguredLLMClient)


class TestModelNameSanitizer:
    """The model string is provider-controlled and reaches the audit log."""

    def test_real_model_identifiers_pass(self):
        from src.services.llm_client import sanitize_model_name

        for name in ("claude-sonnet-4-5", "gpt-4o-mini", "local-cli", "meta-llama/Llama-3-8b"):
            assert sanitize_model_name(name) == name, name

    def test_credential_shapes_are_rejected(self):
        from src.services.llm_client import sanitize_model_name

        for name in (
            "sk-ant-api03-" + "A" * 95,  # provider key: too long
            "sk-" + "a" * 30,  # detector: openai_key
            "eyJhbGciOiJIUzI1NiJ9.abcdefghij",  # detector: jwt
            "Bearer " + "a" * 20,  # grammar: whitespace
            "a" * 49,  # length cap
        ):
            assert sanitize_model_name(name) == "unreported", name

    def test_non_string_is_rejected(self):
        from src.services.llm_client import sanitize_model_name

        assert sanitize_model_name(123) == "unreported"
        assert sanitize_model_name(None) == "unreported"
        assert sanitize_model_name({"a": 1}) == "unreported"

    def test_audit_event_carries_the_sanitized_name(self, monkeypatch):
        import src.nodes.main_node as main_node

        events: list = []
        monkeypatch.setattr(main_node, "emit_trace_event", lambda t, p, s: events.append((t, p)))
        node = MainNode(
            llm_client=_StubLLM('{"alert":"A","advisory":"B","trend":""}', model="sk-ant-api03-" + "A" * 95)
        )
        node.execute({"classified_incidents": [{"id": "i", "location": "A", "type": "fire"}]})
        emitted = [p for t, p in events if t == "safety_content_generated"]
        assert emitted and emitted[0]["model"] == "unreported"
        assert "A" * 95 not in json.dumps(events)


class TestPromptContract:
    """Deleting a safety rule from the prompt must not pass silently.

    The expected strings are written here independently of the production
    constant, so the test states the requirement rather than echoing it.
    """

    def _prompt(self) -> str:
        return build_safety_content_messages(
            classified_incidents=[], severity_scores=[], historical_incidents=[], locale="ja"
        )[0]["content"]

    def test_forbids_re_judging_the_deterministic_severity(self):
        prompt = self._prompt()
        assert "re-judge" in prompt
        assert "severity" in prompt

    def test_requires_every_high_severity_incident_with_its_location(self):
        prompt = self._prompt()
        assert "high" in prompt
        assert "location" in prompt

    def test_forbids_asserting_a_legal_reporting_obligation(self):
        prompt = self._prompt()
        assert "legally" in prompt
        assert "statute" in prompt

    def test_user_payload_carries_the_classified_incidents(self):
        messages = build_safety_content_messages(
            classified_incidents=[{"id": "i1", "category": "fire_hazard"}],
            severity_scores=[],
            historical_incidents=[],
            locale="ja",
        )
        payload = json.loads(messages[1]["content"])
        assert payload["classified_incidents"] == [{"id": "i1", "category": "fire_hazard"}]


class TestDegradedOrdering:
    def test_highest_severity_is_rendered_first(self):
        """The header tells the reader to check the highest severity first."""
        alert = build_degraded_safety_content(severity_scores=_scores())["alert"]
        assert alert.index("HIGH") < alert.index("MEDIUM")


class TestLocaleReachesTheClient:
    def test_payload_locale_is_passed_through_the_graph(self):
        """A hard-coded locale in MainNode would break multilingual output."""
        domain = _DomainClient()
        agent = RETInStoreSafetyAlertAgent(config={"llm": domain})
        agent.compile()
        payload = json.dumps({"locale": "vi", "incidents": [{"location": "Aisle 7", "type": "shelf collapse"}]})
        agent.invoke(payload, ctx=_vi_ctx())
        assert domain.calls and domain.calls[0]["locale"] == "vi"


def _vi_ctx():
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel

    return InvocationContext(session_id="locale-test", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, caller_id="t")


# ── S-3 preservation variant ──────────────────────────────────────────────
#
# Expected values are literals here, and the alert texts below are written the
# way a real model writes them (including the translated Vietnamese forms seen
# in the 2026-08-28 live run), not generated from the production helpers.


def _mixed_scores():
    return [
        {"id": "a", "location": "1F バックヤード 配電盤脇", "category": "fire_hazard", "score": 10, "severity": "high"},
        {"id": "b", "location": "Aisle 7 (household goods)", "category": "structural", "score": 8, "severity": "high"},
        {"id": "c", "location": "Main entrance", "category": "slip_zone", "score": 6, "severity": "high"},
        {"id": "d", "location": "2F 非常口 B", "category": "egress_obstruction", "score": 4, "severity": "medium"},
    ]


class TestLocationAnchors:
    def test_identifier_tokens_are_anchors(self):
        from src.services.llm_client import location_anchors

        # Anchors are normalised (NFKC + casefold) so width/case cannot fake an
        # omission; "B1" and "b1" and "Ｂ１" are the same anchor.
        assert location_anchors("B1 食品売場 冷蔵ケース前") == ["b1"]
        assert location_anchors("2F 非常口 B") == ["2f"]
        assert location_anchors("Aisle 7 (household goods)") == ["7"]
        assert location_anchors("通路７") == ["7"]

    def test_descriptive_names_have_no_anchor(self):
        from src.services.llm_client import location_anchors

        assert location_anchors("Main entrance") == []
        assert location_anchors("") == []
        assert location_anchors(None) == []


class TestHighSeverityCoverage:
    def test_translated_alert_still_verifies(self):
        """🔴 The live vi run rendered "Aisle 7" as "Lối đi 7" — verbatim
        matching would have rejected a perfectly good alert."""
        from src.services.llm_client import verify_high_severity_coverage

        alert = (
            "Có 3 sự cố mức độ cao: tại 1F バックヤード 配電盤脇 có khói; "
            "tại Lối đi 7 (hàng gia dụng) giá đỡ kệ bị sập; "
            "tại Lối vào chính sàn ướt."
        )
        missing, unverifiable = verify_high_severity_coverage(alert, _mixed_scores())
        assert missing == []
        assert unverifiable == ["Main entrance"]

    def test_dropped_high_severity_is_detected(self):
        from src.services.llm_client import verify_high_severity_coverage

        alert = "Chỉ có Lối vào chính bị ướt; tại 1F バックヤード 配電盤脇 có khói."
        missing, _ = verify_high_severity_coverage(alert, _mixed_scores())
        assert missing == ["Aisle 7 (household goods)"]

    def test_medium_severity_is_not_required(self):
        from src.services.llm_client import verify_high_severity_coverage

        alert = "1F バックヤード 配電盤脇 / Lối đi 7 / Lối vào chính"
        missing, _ = verify_high_severity_coverage(alert, _mixed_scores())
        assert missing == []  # "2F 非常口 B" is medium and absent — not a violation

    def test_unverifiable_is_never_reported_as_verified(self):
        """A location with no anchor must not be silently counted as covered."""
        from src.services.llm_client import verify_high_severity_coverage

        scores = [{"location": "Main entrance", "severity": "high"}]
        missing, unverifiable = verify_high_severity_coverage("totally unrelated text", scores)
        assert missing == []
        assert unverifiable == ["Main entrance"]

    def test_anchor_match_respects_alphanumeric_boundaries(self):
        """ "17 people" must not satisfy the anchor "7" of "Aisle 7"."""
        from src.services.llm_client import verify_high_severity_coverage

        scores = [{"location": "Aisle 7", "severity": "high"}]
        missing, _ = verify_high_severity_coverage("17 people were affected", scores)
        assert missing == ["Aisle 7"]
        missing2, _ = verify_high_severity_coverage("aisle 7 is blocked", scores)
        assert missing2 == []

    def test_severity_comes_from_the_deterministic_layer(self):
        """The model's own severity opinion is not consulted — only the scores."""
        from src.services.llm_client import verify_high_severity_coverage

        scores = [{"location": "Aisle 7", "severity": "high", "score": 8}]
        missing, _ = verify_high_severity_coverage("all clear, nothing to report", scores)
        assert missing == ["Aisle 7"]


class TestPreservationDegradesRatherThanBlocks:
    def _state(self):
        return {
            "classified_incidents": [
                {"id": "b", "location": "Aisle 7", "type": "shelf collapse", "injury": True},
                {"id": "a", "location": "Entrance", "type": "wet floor", "people_affected": 6},
            ],
            "locale": "vi",
        }

    def test_all_clear_answer_is_replaced_by_the_deterministic_rendering(self):
        node = MainNode(llm_client=_StubLLM('{"alert":"All clear.","advisory":"B","trend":""}'))
        res = node.execute(self._state())
        # The alert still goes out — an alert naming the hazards beats no alert.
        assert res["status"] == AgentStatus.SUCCESS.value
        assert res["degradation_reason"] == "high_severity_omitted"
        assert "Aisle 7" in res["safety_alert"]
        assert "All clear" not in res["safety_alert"]

    def test_compliant_answer_is_left_alone(self):
        node = MainNode(llm_client=_StubLLM('{"alert":"Nguy hiem tai Lối đi 7 va Entrance","advisory":"B","trend":""}'))
        res = node.execute(self._state())
        assert res["degradation_reason"] == ""
        assert "Lối đi 7" in res["safety_alert"]

    def test_audit_event_reports_counts_not_locations(self, monkeypatch):
        import src.nodes.main_node as main_node

        events: list = []
        monkeypatch.setattr(main_node, "emit_trace_event", lambda t, p, s: events.append((t, p)))
        MainNode(llm_client=_StubLLM('{"alert":"All clear.","advisory":"B","trend":""}')).execute(self._state())
        payload = [p for t, p in events if t == "safety_content_generated"][0]
        # The model's own prose dropped the hazard — that is the signal recorded.
        assert payload["high_severity_omitted_by_model"] == 1
        assert "Aisle 7" not in json.dumps(events, ensure_ascii=False)


class TestPreservationGateBackstop:
    """The hook must fail closed; it is the last line if execute() has a bug."""

    def test_gate_raises_when_a_hazard_is_missing(self):
        node = MainNode(llm_client=_DomainClient())
        with pytest.raises(RuntimeError, match="preservation gate"):
            node._extra_security_gate_output(
                {
                    "status": AgentStatus.SUCCESS.value,
                    "safety_alert": "All clear.",
                    "severity_scores": [{"location": "Aisle 7", "severity": "high"}],
                }
            )

    def test_gate_passes_a_compliant_result(self):
        node = MainNode(llm_client=_DomainClient())
        payload = {
            "status": AgentStatus.SUCCESS.value,
            "safety_alert": "Hazard at Aisle 7",
            "severity_scores": [{"location": "Aisle 7", "severity": "high"}],
        }
        assert node._extra_security_gate_output(payload) is payload

    def test_gate_fails_closed_when_fields_are_absent(self):
        """A dropped field is an anomaly — never "nothing to verify"."""
        node = MainNode(llm_client=_DomainClient())
        for payload in (
            {"status": AgentStatus.SUCCESS.value, "safety_alert": "x"},
            {"status": AgentStatus.SUCCESS.value, "severity_scores": []},
        ):
            with pytest.raises(RuntimeError, match="preservation gate"):
                node._extra_security_gate_output(payload)

    def test_gate_passes_the_obs1_upstream_error_passthrough(self):
        node = MainNode(llm_client=_DomainClient())
        assert node._extra_security_gate_output({}) == {}

    def test_gate_passes_an_upstream_error_result(self):
        node = MainNode(llm_client=_DomainClient())
        payload = {"status": AgentStatus.ERROR.value}
        assert node._extra_security_gate_output(payload) is payload

    def test_gate_message_carries_no_caller_text(self):
        node = MainNode(llm_client=_DomainClient())
        secret_location = "SECRET-CUSTOMER-SITE-9"
        with pytest.raises(RuntimeError) as exc:
            node._extra_security_gate_output(
                {
                    "status": AgentStatus.SUCCESS.value,
                    "safety_alert": "nothing here",
                    "severity_scores": [{"location": secret_location, "severity": "high"}],
                }
            )
        assert secret_location not in str(exc.value)


class TestPreservationEndToEnd:
    def test_dropped_hazard_degrades_through_the_full_graph(self):
        agent = RETInStoreSafetyAlertAgent(config={"llm": _StubLLM('{"alert":"All clear.","advisory":"B","trend":""}')})
        agent.compile()
        payload = json.dumps({"locale": "ja", "incidents": [{"location": "Aisle 7", "type": "fire hazard"}]})
        out = agent.invoke(payload, ctx=_vi_ctx())
        assert out["status"] == AgentStatus.SUCCESS.value
        body = out["output"]
        assert body["degradation_reason"] == "high_severity_omitted"
        assert "Aisle 7" in body["alert"]
        # The pre-existing deterministic mitigation is untouched.
        assert body["incident_log"]["incident_count"] == 1
        assert body["incident_log"]["severity_summary"] == {"high": 1}


# ── 2026-08-29 adversarial re-review — gate hardening ─────────────────────


class TestAnchorReflexivity:
    """🔴 An anchor that cannot match its own source text is an outage.

    The first cut extracted `EZZANINE2` from `MEZZANINE2`; the boundary rule then
    refused it against its own location, so the incident read as missing, the
    deterministic fallback (which quotes locations verbatim) failed the same way,
    and the backstop raised — `status: error`, no alert, for a detected fire.
    """

    LOCATIONS = [
        "MEZZANINE2",
        "Aisle 123456",
        "B1 食品売場 冷蔵ケース前",
        "2F 非常口 B",
        "Aisle 7 (household goods)",
        "b1",
        "通路７",
        "Dock-12A",
        "SECTION-A-4-NORTH-WING-LEVEL2",
    ]

    def test_every_anchor_matches_its_own_location(self):
        from src.services.llm_client import location_anchors, verify_high_severity_coverage

        for location in self.LOCATIONS:
            anchors = location_anchors(location)
            assert anchors, f"no anchor extracted for {location!r}"
            missing, unverifiable = verify_high_severity_coverage(
                f"Hazard reported at {location}", [{"location": location, "severity": "high"}]
            )
            assert missing == [], f"{location!r} cannot match itself (anchors={anchors})"
            assert unverifiable == []

    def test_long_identifier_does_not_end_in_a_blocked_alert(self):
        """End-to-end: the fallback must be able to pass the gate it just failed."""
        agent = RETInStoreSafetyAlertAgent(
            config={"llm": _StubLLM('{"alert":"Hazard at MEZZANINE2","advisory":"B","trend":""}')}
        )
        agent.compile()
        out = agent.invoke(
            json.dumps({"incidents": [{"location": "MEZZANINE2", "type": "fire hazard"}]}),
            ctx=_vi_ctx(),
        )
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["output"]["alert"]
        assert out["output"]["degradation_reason"] == ""


class TestAnchorNormalisation:
    def test_case_and_width_differences_are_not_omissions(self):
        from src.services.llm_client import verify_high_severity_coverage

        missing, _ = verify_high_severity_coverage(
            "Danger at B1 and 通路7",
            [{"location": "b1", "severity": "high"}, {"location": "通路７", "severity": "high"}],
        )
        assert missing == []

    def test_non_ascii_digit_systems_fall_to_the_guaranteed_path(self):
        """Anchors are ASCII by construction — an ASCII identifier is what
        survives translation. A location numbered in another digit system has no
        anchor, so it is `unverifiable` and gets the deterministic roster rather
        than a false "verified"."""
        from src.services.llm_client import location_anchors, verify_high_severity_coverage

        assert location_anchors("قاعة ٧") == []
        missing, unverifiable = verify_high_severity_coverage(
            "All clear.", [{"location": "قاعة ٧", "severity": "high"}]
        )
        assert missing == []
        assert unverifiable == ["قاعة ٧"]


class TestScoresAreNotMutableByTheClient:
    def test_client_cannot_empty_the_authoritative_scores(self):
        """A duck-typed client received the live list by reference."""

        class _Saboteur:
            def generate_safety_content(self, *, severity_scores, classified_incidents, **kwargs):
                severity_scores.clear()
                classified_incidents.clear()
                return {"alert": "All clear.", "advisory": "B", "trend": ""}

        res = MainNode(llm_client=_Saboteur()).execute(
            {"classified_incidents": [{"id": "i", "location": "Aisle 7", "type": "fire hazard"}]}
        )
        assert res["degradation_reason"] == "high_severity_omitted"
        assert res["severity_scores"], "the authoritative scores were emptied"
        assert "Aisle 7" in res["safety_alert"]


class TestUnverifiableGetsDeterministicFacts:
    """ "Cannot verify" must not silently become "accepted"."""

    def _state(self, locations):
        return {
            "classified_incidents": [
                {"id": str(i), "location": loc, "type": "fire hazard", "injury": True}
                for i, loc in enumerate(locations)
            ],
            "locale": "vi",
        }

    def test_anchorless_hazard_is_appended_to_an_all_clear_answer(self):
        res = MainNode(llm_client=_StubLLM('{"alert":"All clear.","advisory":"B","trend":""}')).execute(
            self._state(["Main entrance"])
        )
        assert res["degradation_reason"] == ""  # not verifiable, so not an omission
        assert "Main entrance" in res["safety_alert"]
        assert "HIGH" in res["safety_alert"]

    def test_roster_is_appended_even_when_the_prose_looks_fine(self):
        """🔴 Token matching cannot tell "at Aisle 7" from "all clear at Aisle 7".

        The roster is what actually carries the guarantee, so it is appended
        unconditionally on the non-degraded path.
        """
        res = MainNode(llm_client=_StubLLM('{"alert":"All clear at Loi di 7","advisory":"B","trend":""}')).execute(
            self._state(["Aisle 7"])
        )
        assert res["degradation_reason"] == ""
        roster = res["safety_alert"].split("Detected high-severity locations")[-1]
        assert "Aisle 7" in roster

    def test_roster_lists_every_high_severity_incident(self):
        res = MainNode(
            llm_client=_StubLLM('{"alert":"Nguy hiem tai Loi di 7 va o cua chinh","advisory":"B","trend":""}')
        ).execute(self._state(["Aisle 7", "Main entrance"]))
        roster = res["safety_alert"].split("Detected high-severity locations")[-1]
        assert "Main entrance" in roster
        assert "Aisle 7" in roster
        assert roster.count("- HIGH") == 2

    def test_roster_rows_carry_the_deterministic_category(self):
        res = MainNode(llm_client=_StubLLM('{"alert":"x at Loi di 7","advisory":"B","trend":""}')).execute(
            self._state(["Aisle 7"])
        )
        roster = res["safety_alert"].split("Detected high-severity locations")[-1]
        assert "fire_hazard" in roster
        assert "(score 10)" in roster


class TestMultipleAndCompoundLocations:
    def test_every_high_severity_incident_is_checked(self):
        """Kills a `severity_scores[:2]` truncation."""
        from src.services.llm_client import verify_high_severity_coverage

        scores = [
            {"location": "Aisle 3", "severity": "high"},
            {"location": "Aisle 5", "severity": "high"},
            {"location": "Aisle 9", "severity": "high"},
        ]
        missing, _ = verify_high_severity_coverage("hazards at aisle 3 and aisle 5", scores)
        assert missing == ["Aisle 9"]

    def test_any_anchor_suffices_for_a_compound_location(self):
        """Kills `anchors[:1]` and an `any` -> `all` tightening.

        A translated alert may keep only one identifier from a compound name.
        """
        from src.services.llm_client import location_anchors, verify_high_severity_coverage

        location = "Building 4 / Aisle 12"
        assert len(location_anchors(location)) == 2
        missing, _ = verify_high_severity_coverage(
            "nguy hiem tai Aisle 12", [{"location": location, "severity": "high"}]
        )
        assert missing == []
        missing2, _ = verify_high_severity_coverage(
            "nguy hiem tai Building 4", [{"location": location, "severity": "high"}]
        )
        assert missing2 == []


class TestSaboteurClientCannotDestroyTheIncidentLog:
    """The deterministic `incident_log` is the mitigation that holds when the
    prose cannot be trusted. A client that mutated the caller's list by
    reference emptied it out of State, so post_process reported
    `incident_count: 0` for a real high-severity fire — the mitigation itself
    disappeared. Measured 2026-08-29.
    """

    def test_incident_log_survives_a_mutating_client(self):
        class _Saboteur:
            def generate_safety_content(self, *, classified_incidents, severity_scores, **kwargs):
                classified_incidents.clear()
                severity_scores.clear()
                return {"alert": "Hazard at Aisle 7", "advisory": "B", "trend": ""}

        agent = RETInStoreSafetyAlertAgent(config={"llm": _Saboteur()})
        agent.compile()
        out = agent.invoke(
            json.dumps({"incidents": [{"location": "Aisle 7", "type": "fire hazard"}]}),
            ctx=_vi_ctx(),
        )
        log = out["output"]["incident_log"]
        assert log["incident_count"] == 1
        assert log["by_location"] == {"Aisle 7": 1}
        assert log["by_category"] == {"fire_hazard": 1}


class TestRosterIsNotDuplicatedOnTheDegradedPath:
    """The deterministic rendering already names every hazard verbatim, so the
    roster must not repeat one of them — a store employee would read the same
    location twice as two separate findings."""

    def test_degraded_alert_lists_each_hazard_once(self):
        class _Useless:
            def generate_safety_content(self, **kwargs):
                return {"alert": "Nothing relevant here.", "advisory": "B", "trend": ""}

        res = MainNode(llm_client=_Useless()).execute(
            {
                "classified_incidents": [
                    {"id": "a", "location": "Aisle 7", "type": "fire hazard", "injury": True},
                    {"id": "b", "location": "Main entrance", "type": "fire hazard", "injury": True},
                ]
            }
        )
        assert res["degradation_reason"] == "high_severity_omitted"
        alert = res["safety_alert"]
        # Both hazards are present, each exactly once.
        assert alert.count("Aisle 7") == 1
        assert alert.count("Main entrance") == 1
        assert "Detected high-severity locations" not in alert


class TestAnchorReflexivityUnderLength:
    """🔴 `{1,64}` CHUNKS a longer alphanumeric run rather than skipping it, so a
    65-character run yields a tail chunk that cannot match its own source. With
    no filter that produced `status: error` and no alert at all. Property-style
    across the boundary, because the example-based test missed exactly this.
    """

    def test_runs_around_the_64_char_boundary_never_report_missing(self):
        from src.services.llm_client import verify_high_severity_coverage

        for n in range(58, 72):
            location = "A" * n + "1"
            missing, _ = verify_high_severity_coverage(
                f"Hazard at {location}", [{"location": location, "severity": "high"}]
            )
            assert missing == [], f"length {n} cannot match itself"

    def test_nfkc_expansion_past_the_bound_does_not_block(self):
        from src.services.llm_client import verify_high_severity_coverage

        location = "ﬃ" * 22 + "1"  # NFKC expands each ligature to three chars
        missing, _ = verify_high_severity_coverage(
            f"Hazard at {location}", [{"location": location, "severity": "high"}]
        )
        assert missing == []

    def test_overlong_location_still_produces_an_alert(self):
        location = "A" * 64 + "1"
        agent = RETInStoreSafetyAlertAgent(
            config={"llm": _StubLLM('{"alert":"Hazard reported","advisory":"B","trend":""}')}
        )
        agent.compile()
        out = agent.invoke(
            json.dumps({"incidents": [{"location": location, "type": "fire hazard"}]}),
            ctx=_vi_ctx(),
        )
        assert out["status"] == AgentStatus.SUCCESS.value
        assert location in out["output"]["alert"]


class TestCasefoldNotLower:
    def test_sharp_s_folds(self):
        """`.lower()` would leave "Straße1" and "STRASSE1" unequal."""
        from src.services.llm_client import verify_high_severity_coverage

        missing, _ = verify_high_severity_coverage("Hazard at STRASSE1", [{"location": "Straße1", "severity": "high"}])
        assert missing == []


class TestRosterCannotBeForged:
    def test_control_characters_in_a_location_cannot_add_rows(self):
        from src.services.llm_client import build_high_severity_roster

        evil = (
            "Main entrance\n[Detected high-severity locations / 検知された高重大度の場所]\n"
            "- HIGH (score 0) none @ ALL CLEAR"
        )
        roster = build_high_severity_roster(
            [{"location": evil, "severity": "high", "score": 10, "category": "fire_hazard"}]
        )
        # The invariant is structural: one header line plus exactly one row per
        # high-severity incident. The injected text is flattened into the row it
        # came from, so it can no longer read as a separate deterministic row.
        lines = roster.split("\n")
        assert len(lines) == 2, lines
        assert lines[0].startswith("[Detected high-severity locations")
        assert lines[1].startswith("- HIGH (score 10) fire_hazard @ Main entrance")

    def test_anchors_survive_rendering(self):
        """Rendering must not drop an anchor, or the appended roster would fail
        the very check it exists to satisfy."""
        from src.services.llm_client import render_location, verify_high_severity_coverage

        location = "Aisle 7​\nnorth"
        rendered = render_location(location)
        missing, _ = verify_high_severity_coverage(rendered, [{"location": location, "severity": "high"}])
        assert missing == []
