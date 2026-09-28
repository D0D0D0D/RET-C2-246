"""AgentCore Platform v1.0 — LLM injection seam (S-5).

LLMClient is the boundary between MainNode and the underlying model provider.
API keys are resolved via ``ctx.secrets.require(...)`` by the deploy wiring and
passed into a provider-backed client — NEVER read from ``os.environ`` and NEVER
stored in agent State (PB-2 / S-5).

Injection contract (project standard)
-----------------------------------
The client arrives through ``config["llm"]``:
``Graph(config={"llm": <BaseLLM>})`` -> ``register_nodes()`` ->
``MainNode(llm_client=...)`` -> ``resolve_llm_client()``.

🔴 Before 2026-08-28 the only way to reach ``MainNode._llm`` was the node's own
``llm_client=`` argument, and ``register_nodes()`` constructed ``MainNode()``
with no arguments — so nothing outside the node could ever supply a client and
the default build always hit ``UnconfiguredLLMClient``. Tests were green only
because they rewrote the private ``_nodes["main"]._llm`` attribute directly.

Protocol gap
------------
- What the platform injects: ``BaseLLM`` (``complete(messages) -> dict``).
- What this agent's node calls: ``generate_safety_content(...) -> dict``.

``BaseLLMSafetyContentAdapter`` bridges the two, and it lives **in src/** — an
adapter that exists only in ``tests/`` leaves production with no adapter at all
(that is exactly the state this module is fixing).

Safety posture
--------------
- Anomaly detection and severity are decided **deterministically** in
  ``severity_service`` and are never re-judged by the model. The model only
  writes prose for facts that have already been established.
- ``finalize_safety_content()`` applies deterministic guards to whatever any
  client returns (empty trend without history, mandatory non-binding
  disclaimer), so a directly injected domain client cannot skip them.
  🔴 The appended disclaimer is fixed ja/en text — the only wording this agent
  can guarantee without a model. A live run in Vietnamese (2026-08-28) showed a
  vi advisory carrying a ja/en-only caveat, so the prompt now also asks for the
  same caveat in the requested locale. That half is model-written and therefore
  NOT guaranteed; the ja/en text remains the authoritative backstop.
- ``build_degraded_safety_content()`` renders the deterministic detection result
  when the model is reachable but unusable, so a provider outage never silences
  a detected hazard.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Protocol

# Fields of the generated content contract. ``trend`` is optional in a model
# response (it is empty whenever no history was supplied).
REQUIRED_CONTENT_FIELDS: tuple[str, ...] = ("alert", "advisory")
CONTENT_FIELDS: tuple[str, ...] = ("alert", "advisory", "trend")

# Fixed vocabulary for an unusable response. Never carry provider text into
# State — a provider's rejection message can echo the request body verbatim.
REASON_EMPTY_CONTENT = "empty_content"
REASON_NOT_JSON = "not_json"
REASON_SCHEMA_MISMATCH = "schema_mismatch"
REASON_EMPTY_ALERT = "empty_alert"
REASON_UNSPECIFIED = "unspecified"
# The generated prose dropped a high-severity incident the deterministic layer
# found. Not a provider fault — the model answered, but unusably for safety.
REASON_HIGH_SEVERITY_OMITTED = "high_severity_omitted"
# 🔴 A directly injected domain client can raise SafetyContentUnusableError with
# any string. MainNode maps anything outside this set to REASON_UNSPECIFIED, so
# an attacker-influenced value can never become part of State.
KNOWN_UNUSABLE_REASONS: frozenset[str] = frozenset(
    {REASON_EMPTY_CONTENT, REASON_NOT_JSON, REASON_SCHEMA_MISMATCH, REASON_EMPTY_ALERT}
)

# Appended deterministically to every advisory. The reporting advisory must
# never assert a legal obligation, and a prompt instruction is not enforcement.
NON_BINDING_DISCLAIMER = (
    "※本アドバイザリは法的判断ではありません。適用される法令・届出先は責任者が確認してください。"
    " / This advisory is not a legal determination; the applicable law and the "
    "authority to notify must be confirmed by a responsible person."
)

SAFETY_CONTENT_SYSTEM_PROMPT = (
    "You write on-site safety wording for retail store staff who are not safety "
    "experts. The incident records, their anomaly category and their severity "
    "have ALREADY been decided by a deterministic rule engine. "
    "Rules you must follow: "
    "(1) Do NOT re-judge whether something is an anomaly, and do NOT change, "
    "soften or raise any severity — restate what you are given. "
    "(2) Do NOT invent incidents, locations, causes or measurements that are "
    "not in the supplied records. "
    "(3) Every incident whose severity is 'high' MUST be mentioned together "
    "with its location. "
    "(4) 'advisory' must be non-binding: never state that reporting is legally "
    "required, and never name a specific statute or deadline. End 'advisory' "
    "with one short sentence, IN THE REQUESTED LOCALE, saying that this is not "
    "a legal determination and that a responsible person must confirm the "
    "applicable law and the authority to notify. "
    "(5) 'trend' must be an empty string when no historical_incidents are "
    "supplied. "
    "(6) Write every field in the requested locale. "
    "(7) Return ONLY one JSON object with the string fields alert, advisory and "
    "trend. No Markdown, no code fence, no commentary."
)

# Rendered when the model is reachable but its answer is unusable. Bilingual and
# locale-independent on purpose: fabricating fluent prose in an arbitrary target
# language without a model would be worse than showing the facts untranslated.
_DEGRADED_ALERT_HEADER = (
    "[SAFETY ALERT / 安全アラート]\n"
    "Automatic multilingual wording is unavailable. The deterministic detection "
    "result is shown as-is; check the highest severity first.\n"
    "自動多言語生成が利用できません。検知結果をそのまま表示します。"
    "重大度の高いものから確認してください。"
)
_DEGRADED_ALERT_NO_INCIDENT = "No incident was scored. / 採点対象のインシデントはありません。"
_DEGRADED_ADVISORY = (
    "Automatic wording is unavailable. A responsible person should review the "
    "detected incidents above and decide what to do next.\n"
    "自動生成が利用できません。上記の検知結果を責任者が確認し、対応を判断してください。"
)


class SafetyContentUnusableError(RuntimeError):
    """The model answered, but the answer cannot be used as safety content.

    ``code`` is drawn from the fixed vocabulary above — the provider's own
    message is used for nothing and is never stored.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LLMClient(Protocol):
    """Structural type for an injectable safety-content generator."""

    def generate_safety_content(
        self,
        *,
        classified_incidents: list[dict[str, Any]],
        severity_scores: list[dict[str, Any]],
        historical_incidents: list[dict[str, Any]],
        locale: str,
    ) -> dict[str, str]:
        """Generate on-site safety content in ``locale``.

        Returns a dict with three string keys (plus an optional ``model``
        naming the model that actually answered):
          - ``alert``    : on-site safety alert for non-expert staff
          - ``advisory`` : reporting advisory (MUST be framed as non-binding —
                           never assert a legal reporting obligation; tell the
                           reader that the applicable law / authority must be
                           confirmed by a human). ``finalize_safety_content()``
                           enforces this with a fixed disclaimer.
          - ``trend``    : location-wise risk trend (empty string if no
                           ``historical_incidents`` were provided)
        """
        ...


class UnconfiguredLLMClient:
    """Fail-closed default. Replace via ``config["llm"]`` in production.

    A deploy with no client is a provisioning mistake, not a transient fault:
    silently degrading it would leave a safety agent permanently producing
    machine-rendered text with nobody noticing. MainNode lets this
    ``NotImplementedError`` propagate (``status: error``) instead of degrading.
    """

    def generate_safety_content(
        self,
        *,
        classified_incidents: list[dict[str, Any]],
        severity_scores: list[dict[str, Any]],
        historical_incidents: list[dict[str, Any]],
        locale: str,
    ) -> dict[str, str]:
        raise NotImplementedError(
            "No LLMClient configured. Inject one via config={'llm': <BaseLLM>} "
            "(keys via ctx.secrets.require) before invoking."
        )


def build_safety_content_messages(
    *,
    classified_incidents: list[dict[str, Any]],
    severity_scores: list[dict[str, Any]],
    historical_incidents: list[dict[str, Any]],
    locale: str,
) -> list[dict[str, str]]:
    """Build the chat messages for a ``BaseLLM.complete()`` call."""
    payload = {
        "locale": locale,
        "severity_scores": severity_scores,
        "classified_incidents": classified_incidents,
        "historical_incidents": historical_incidents,
    }
    return [
        {"role": "system", "content": SAFETY_CONTENT_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


# Bounded quantifier: an unbounded `.*` here is a ReDoS risk on a large model
# response (quadratic backtracking measured in seconds at input-size limits).
_JSON_OBJECT = re.compile(r"\{.{0,200000}\}", re.DOTALL)


def parse_safety_content(raw: str) -> dict[str, str]:
    """Parse a model response into the content contract.

    Raises :class:`SafetyContentUnusableError` (fixed code, no provider text)
    rather than returning something half-formed — an unusable answer must reach
    the deterministic fallback, not the caller.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    match = _JSON_OBJECT.search(text.strip())
    if not match:
        raise SafetyContentUnusableError(REASON_NOT_JSON)
    try:
        parsed = json.loads(match.group(0))
    except (ValueError, TypeError):
        raise SafetyContentUnusableError(REASON_NOT_JSON) from None
    if not isinstance(parsed, dict):
        raise SafetyContentUnusableError(REASON_SCHEMA_MISMATCH)
    for field in REQUIRED_CONTENT_FIELDS:
        if not isinstance(parsed.get(field), str):
            raise SafetyContentUnusableError(REASON_SCHEMA_MISMATCH)
    trend = parsed.get("trend", "")
    if not isinstance(trend, str):
        raise SafetyContentUnusableError(REASON_SCHEMA_MISMATCH)
    if not parsed["alert"].strip():
        # An empty alert is worse than the deterministic rendering: it reads as
        # "nothing to worry about" for an incident that was actually detected.
        raise SafetyContentUnusableError(REASON_EMPTY_ALERT)
    # Extra keys the model invented are dropped, never propagated.
    return {"alert": parsed["alert"], "advisory": parsed["advisory"], "trend": trend}


def finalize_safety_content(content: dict[str, str], *, has_history: bool) -> dict[str, str]:
    """Apply the deterministic output guards to any client's content.

    Applied in MainNode, not inside the adapter, so that a directly injected
    domain-protocol client cannot bypass them:

    - ``trend`` is forced empty when no history was supplied (the model must not
      claim a trend it had no data for);
    - the non-binding disclaimer is always present on ``advisory`` (a legal
      framing must not depend on the model obeying a prompt).
    """
    alert = str(content.get("alert") or "").strip()
    advisory = str(content.get("advisory") or "").strip()
    trend = str(content.get("trend") or "").strip()
    if not has_history:
        trend = ""
    if NON_BINDING_DISCLAIMER not in advisory:
        advisory = f"{advisory}\n{NON_BINDING_DISCLAIMER}".strip()
    return {"alert": alert, "advisory": advisory, "trend": trend}


def build_degraded_safety_content(*, severity_scores: list[dict[str, Any]]) -> dict[str, str]:
    """Render the deterministic detection result as safety content.

    Used when a client was injected but could not produce usable prose. The
    detection facts (location / category / severity) are already established
    without the model, so a provider fault must not turn a detected fire hazard
    into no alert at all.
    """
    lines = [
        "- {severity} (score {score}) {category} @ {location}".format(
            severity=str(s.get("severity", "unknown")).upper(),
            score=s.get("score", "?"),
            category=s.get("category", "other"),
            location=render_location(s.get("location", "")),
        )
        for s in severity_scores
    ]
    body = "\n".join(lines) if lines else _DEGRADED_ALERT_NO_INCIDENT
    return {
        "alert": f"{_DEGRADED_ALERT_HEADER}\n{body}",
        "advisory": _DEGRADED_ADVISORY,
        "trend": "",
    }


# ── S-3 preservation check (the preservation variant of the output gate) ─────
#
# The deterministic layer decides WHICH incidents are high severity; the model
# only writes prose about them. If a high-severity incident vanishes from that
# prose, the alert reads as though it was never detected — the exact failure a
# safety agent must not have. The check below verifies preservation; it never
# filters, and it never re-judges severity.
#
# Matching across languages is the hard part. This agent's whole point is
# generating in the caller's locale, and a location name is often translated:
# a live vi run rendered "Aisle 7 (household goods)" as "Lối đi 7 (hàng gia
# dụng)". Verbatim matching would have rejected a perfectly good alert.
#
# What survives translation is the IDENTIFIER part of a location — floor codes,
# aisle numbers, door letters ("B1", "3F", "7"). Descriptive names do not:
# "Main entrance" became "Lối vào chính", which shares nothing with the source.
#
# 🔴 Hence three outcomes per incident, not two. A location with no identifier
# token is reported as **unverifiable**, never as verified. Treating "I could
# not check this" as "this is fine" is the failure mode this comment exists to
# prevent.

HIGH_SEVERITY = "high"

# 🔴 Anchors are MAXIMAL alphanumeric runs, not a hand-shaped identifier pattern.
# The earlier `[A-Za-z]{0,8}\d{1,4}[A-Za-z]{0,8}` truncated long identifiers —
# "MEZZANINE2" yielded the anchor "EZZANINE2", which the boundary rule below then
# refused to match against its own source text. The location was reported
# missing, the deterministic fallback was substituted, the fallback contained the
# same unmatchable location, and the backstop raised: `status: error` with NO
# ALERT for a detected fire hazard. A matcher that cannot match its own input
# turns a safety gate into an outage. Every quantifier here is bounded.
# ASCII by design: an ASCII identifier is what survives translation into another
# script. A location numbered in another digit system yields no anchor and is
# reported `unverifiable`, which routes it to the guaranteed roster instead of a
# false "verified".
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]{1,64}")
_HAS_DIGIT = re.compile(r"[0-9]")


def _normalize(text: Any) -> str:
    """NFKC + casefold so width and case differences do not fake an omission.

    Measured false positives without this: a location written "b1" against an
    alert saying "B1", and "通路７" (fullwidth) against "通路7".
    """
    if not isinstance(text, str):
        return ""
    return unicodedata.normalize("NFKC", text).casefold()


def _anchor_in(anchor: str, normalized_text: str) -> bool:
    """Match an already-normalized anchor on an alphanumeric boundary.

    Plain substring matching would let the "7" of "Aisle 7" be satisfied by an
    unrelated "17" elsewhere in the prose — a false *verified*, the dangerous
    direction here.
    """
    pattern = r"(?<![0-9A-Za-z])" + re.escape(anchor) + r"(?![0-9A-Za-z])"
    return re.search(pattern, normalized_text) is not None


def location_anchors(location: Any) -> list[str]:
    """Normalized tokens of *location* expected to survive translation.

    Every returned anchor is guaranteed to match the location it came from. That
    reflexivity is what keeps the deterministic fallback — which quotes locations
    verbatim — able to pass the gate it just failed.
    """
    normalized = _normalize(location)
    if not normalized:
        return []
    anchors = [t for t in _ALNUM_RUN.findall(normalized) if _HAS_DIGIT.search(t)]
    # 🔴 KEEP THIS FILTER. `{1,64}` does not extract maximal runs — it CHUNKS a
    # longer one, so an alphanumeric run of 65+ characters yields a tail chunk
    # preceded by an alphanumeric, which cannot match its own source. Without
    # this filter that location reads as "missing", the deterministic fallback
    # fails for the same reason, and the backstop raises: `status: error` with no
    # alert for a detected hazard. (NFKC can push a shorter input past 64 too —
    # "ﬃ" expands to three characters.) A dropped anchor routes the location to
    # the `unverifiable` path and its guaranteed roster row — the safe direction.
    return [a for a in anchors if _anchor_in(a, normalized)]


def verify_high_severity_coverage(alert: Any, severity_scores: Any) -> tuple[list[str], list[str]]:
    """Check that every high-severity incident survives into *alert*.

    Returns ``(missing, unverifiable)`` location lists:

    - **missing** — the location has an identifier anchor and the anchor is
      absent from the alert. This is a real omission and the caller degrades.
    - **unverifiable** — the location has no anchor to match on, so coverage
      cannot be decided either way. Never counted as verified, and deliberately
      NOT treated as a violation: blocking here would reject the legitimate
      "Main entrance"/"Lối vào chính" case and take a real alert offline.

    ``severity_scores`` is the deterministic scoring output — the model's own
    opinion of severity is not consulted.
    """
    text = _normalize(alert)
    missing: list[str] = []
    unverifiable: list[str] = []
    if not isinstance(severity_scores, list):
        return missing, unverifiable
    for score in severity_scores:
        if not isinstance(score, dict) or score.get("severity") != HIGH_SEVERITY:
            continue
        location = str(score.get("location") or "")
        anchors = location_anchors(location)
        if not anchors:
            unverifiable.append(location)
        elif not any(_anchor_in(a, text) for a in anchors):
            missing.append(location)
    return missing, unverifiable


_UNVERIFIED_ROSTER_HEADER = "[Detected high-severity locations / 検知された高重大度の場所]"


# Control characters, including newlines, plus bidi overrides. A caller-supplied
# location containing "\n[Detected high-severity locations …]\n- HIGH … @ ALL
# CLEAR" would otherwise render as extra rows that look deterministic.
_UNSAFE_IN_ROW = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]{1,4096}")


def render_location(location: Any) -> str:
    """Flatten a caller-supplied location onto a single roster row.

    Only control characters are replaced and nothing is truncated: the rendered
    row must keep every anchor, or the appended roster would fail the very check
    it exists to satisfy.
    """
    return _UNSAFE_IN_ROW.sub(" ", str(location)).strip()


def build_high_severity_roster(severity_scores: Any) -> str:
    """Deterministic roster of EVERY high-severity incident.

    🔴 This, not the text match, is what actually guarantees preservation. Token
    matching can only ask "does this location appear?", and two real cases defeat
    that: a location with no identifier anchor ("Main entrance" becomes "Lối vào
    chính", sharing nothing with the source) cannot be checked at all, and prose
    that names a location while denying it ("All clear at Aisle 7") matches
    happily. Appending the deterministic facts makes the guarantee unconditional
    and language-independent; the text match remains as the signal that the
    model's own prose dropped something and should be degraded.
    """
    if not isinstance(severity_scores, list):
        return ""
    lines = [
        "- {severity} (score {score}) {category} @ {location}".format(
            severity=str(score.get("severity", "unknown")).upper(),
            score=score.get("score", "?"),
            category=score.get("category", "other"),
            location=render_location(score.get("location") or ""),
        )
        for score in severity_scores
        if isinstance(score, dict) and score.get("severity") == HIGH_SEVERITY
    ]
    if not lines:
        return ""
    return _UNVERIFIED_ROSTER_HEADER + "\n" + "\n".join(lines)


class BaseLLMSafetyContentAdapter:
    """Adapt a ``BaseLLM`` (``complete(messages) -> dict``) to this agent's client."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    def generate_safety_content(
        self,
        *,
        classified_incidents: list[dict[str, Any]],
        severity_scores: list[dict[str, Any]],
        historical_incidents: list[dict[str, Any]],
        locale: str,
    ) -> dict[str, str]:
        response = self._llm.complete(
            build_safety_content_messages(
                classified_incidents=classified_incidents,
                severity_scores=severity_scores,
                historical_incidents=historical_incidents,
                locale=locale,
            )
        )
        raw = _content_of(response)
        if not raw.strip():
            raise SafetyContentUnusableError(REASON_EMPTY_CONTENT)
        content = parse_safety_content(raw)
        content["model"] = _model_of(response)
        return content


def _content_of(response: Any) -> str:
    """Assistant text from a canonical ``BaseLLM.complete()`` response.

    ``{"content": None}`` must not become the string ``"None"`` — that would be
    handed to a store employee as the safety alert.
    """
    content = response.get("content") if isinstance(response, dict) else response
    if content is None:
        return ""
    return content if isinstance(content, str) else str(content)


# The model name is provider-controlled and goes straight to the audit log, so
# it is constrained before it is emitted, with bounded quantifiers throughout.
# The length cap is what does most of the work: real model
# identifiers are short (a vendor prefix plus a version), while provider API keys
# are an order of magnitude longer.
MODEL_NAME_MAX_LEN = 48
_SAFE_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,%d}\Z" % (MODEL_NAME_MAX_LEN - 1))
MODEL_NAME_REJECTED = "unreported"


def sanitize_model_name(name: Any) -> str:
    """Return *name* only when it is a plausible model identifier.

    Three filters, because none of them is sufficient alone:

    1. a conservative identifier grammar (rejects whitespace, quotes, prose);
    2. a hard length cap (a real provider key does not fit);
    3. the framework credential detector, reused rather than re-implemented.

    ⚠️ This BOUNDS the exposure, it does not eliminate it: a short
    credential-shaped token that satisfies all three would still be logged. The
    load-bearing control is that this value comes from our own adapter reading
    the canonical response, not from arbitrary caller input. Note also that the
    shipped detector's ``openai_key`` pattern is ``sk-[a-zA-Z0-9]{20,}``, which
    does not match hyphenated Anthropic-style keys — hence the length cap.
    """
    if not isinstance(name, str) or not _SAFE_MODEL_NAME.match(name):
        return MODEL_NAME_REJECTED
    try:
        from shared.security.credential_detector import detect_credentials

        if detect_credentials(name):
            return MODEL_NAME_REJECTED
    except ImportError:  # pragma: no cover — SDK absent
        pass
    return name


def _model_of(response: Any) -> str:
    """The model that actually answered.

    Read from the canonical response, not from config: config names what the
    caller asked for, and reporting it would make the audit log disagree with
    what the injected client really used. ``BaseLLM`` exposes no public model
    attribute, so the response is the only source.
    """
    if isinstance(response, dict):
        name = response.get("model")
        if isinstance(name, str) and name:
            return sanitize_model_name(name)
    return "injected"


def _defines_own_callable(obj: Any, name: str) -> bool:
    """True only when *name* is a real, callable attribute of *obj* or its classes.

    ``getattr()`` alone is deliberately not used for the lookup: an object with
    ``__getattr__`` (the SDK LLM clients delegate that way) answers *every*
    probe, so it cannot tell an implementation from a delegation.

    Callability is then checked as well — a non-callable attribute of the right
    name would otherwise be accepted here and blow up inside ``execute()``,
    where the broad handler turns a provisioning mistake into a *successful*
    degraded run that nobody investigates.
    """
    own = getattr(obj, "__dict__", None)
    found = isinstance(own, dict) and name in own
    if not found:
        found = any(name in klass.__dict__ for klass in type(obj).__mro__)
    if not found:
        return False
    try:
        return callable(getattr(obj, name))
    except Exception:  # noqa: BLE001 — a property that raises is not a usable client
        return False


def resolve_llm_client(injected: Any | None) -> Any:
    """Resolve the value of ``config["llm"]`` into a safety-content client.

    🔴 The ``BaseLLM`` check comes **before** duck typing. SDK clients delegate
    unknown attributes to an inner LangChain client via ``__getattr__``, so a
    ``getattr(x, "generate_safety_content")`` probe can bind to something that
    is not this agent's protocol at all — e.g. a batch-shaped method with a
    different signature. Duck typing therefore only accepts a method that is
    really defined and callable (:func:`_defines_own_callable`).
    """
    if injected is None:
        return UnconfiguredLLMClient()
    try:
        from shared.services.llm.base_llm import BaseLLM

        if isinstance(injected, BaseLLM):
            return BaseLLMSafetyContentAdapter(injected)
    except ImportError:  # pragma: no cover — SDK absent
        pass
    if _defines_own_callable(injected, "generate_safety_content"):
        return injected
    if _defines_own_callable(injected, "complete"):
        return BaseLLMSafetyContentAdapter(injected)
    # Anything else is not a usable client; fail closed rather than crash later
    # with an AttributeError inside execute().
    return UnconfiguredLLMClient()
