"""Shared test fixtures for RET-C2-246.

Provides a deterministic FakeLLMClient so unit/integration tests never call a
real model provider (and never need credentials).
"""

from __future__ import annotations

from typing import Any

import pytest


class FakeLLMClient:
    """Deterministic stand-in for an injected LLMClient."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate_safety_content(
        self,
        *,
        classified_incidents: list[dict[str, Any]],
        severity_scores: list[dict[str, Any]],
        historical_incidents: list[dict[str, Any]],
        locale: str,
    ) -> dict[str, str]:
        self.calls.append({"incidents": len(classified_incidents), "locale": locale})
        n = len(classified_incidents)
        # 🔴 A compliant model names every high-severity location (prompt rule 3),
        # and the S-3 preservation gate checks exactly that. A fake that returned
        # only generic prose would be permanently degraded — "the mock returns a
        # finished product" shape that hides real defects. Mirror the contract.
        high = ", ".join(str(s.get("location", "")) for s in severity_scores if s.get("severity") == "high")
        return {
            "alert": (
                f"[{locale}] {n} location(s) need attention"
                + (f"; high severity at {high}" if high else "")
                + ". Mind wet floors and clear aisles."
            ),
            "advisory": (
                f"[{locale}] Consider whether a report may be appropriate. "
                "This is not a legal determination — the applicable law and "
                "authority must be confirmed by a responsible person."
            ),
            "trend": (f"[{locale}] Recurring risk at top locations." if historical_incidents else ""),
        }


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def sample_payload() -> str:
    import json

    return json.dumps(
        {
            "locale": "ja",
            "incidents": [
                {
                    "id": "i1",
                    "location": "Entrance",
                    "type": "wet floor slip risk",
                    "time": "2026-06-25T10:00",
                    "description": "spill near the door",
                    "people_affected": 6,
                    "injury": False,
                },
                {
                    "id": "i2",
                    "location": "Aisle 3",
                    "type": "fire hazard near shelf",
                    "injury": True,
                },
            ],
        }
    )


# ── 実 LLM 1 回転検証 (work/scripts/mrb_live_llm_check.py) が読む fixture ──────────
#
# ハーネスは config/config.yaml を基底に agent_config を重ね、config["llm"] へ実 LLM を
# 差して Graph(config=...) を構築する。RET-C2-246 の注入シームは config["llm"] なので、
# repo 側で追加の構築依存を渡す必要はない（live_llm_injector も不要 —
# それを使うとアダプタがテスト側にしか存在しない状態を再現してしまう）。
@pytest.fixture
def agent_config() -> dict:
    return {}


@pytest.fixture
def live_input_context() -> dict:
    # tests/integration/test_graph_flow.py は input_context 無しで invoke する。
    return {}


def _incident(**kw) -> dict:
    base = {
        "id": "i0",
        "location": "",
        "type": "",
        "time": "2026-06-25T10:00",
        "description": "",
        "people_affected": 0,
        "injury": False,
    }
    base.update(kw)
    return base


# 🔴 本来の業務入力。汎用ペイロードでは出ない欠陥を出すためのもの。
# 高/中/低の重大度が混在し、historical_incidents があるので trend も生成される。
@pytest.fixture
def realistic_payload_ja() -> str:
    import json

    return json.dumps(
        {
            "locale": "ja",
            "incidents": [
                _incident(
                    id="inc-2026-0625-01",
                    location="B1 食品売場 冷蔵ケース前",
                    type="床の水濡れによる転倒リスク",
                    description="冷蔵ケースの結露で床が濡れており、買物客が滑りかけた",
                    people_affected=6,
                ),
                _incident(
                    id="inc-2026-0625-02",
                    location="2F 非常口 B",
                    type="egress blocked by stacked cartons",
                    description="返品待ちの段ボールが非常口前に積まれ、扉が半分しか開かない",
                    people_affected=0,
                ),
                _incident(
                    id="inc-2026-0625-03",
                    location="1F バックヤード 配電盤脇",
                    type="焦げ臭・発煙",
                    description="配電盤付近で焦げた臭いがし、わずかに白煙が見えた",
                    injury=True,
                    people_affected=2,
                ),
                _incident(
                    id="inc-2026-0625-04",
                    location="3F レジ待機列",
                    type="混雑による通路の狭窄",
                    description="セール時間帯にレジ列が通路を塞いだ",
                    people_affected=30,
                ),
            ],
            "historical_incidents": [
                {"location": "B1 食品売場 冷蔵ケース前", "type": "床の水濡れ", "time": "2026-06-11"},
                {"location": "B1 食品売場 冷蔵ケース前", "type": "床の水濡れ", "time": "2026-06-18"},
                {"location": "2F 非常口 B", "type": "通路の物品滞留", "time": "2026-06-20"},
            ],
        },
        ensure_ascii=False,
    )


# 多言語出力がこの AG の看板なので、非 ja ロケールも実入力として残す。
# history 無し = trend が空になることの実 LLM 側の確認も兼ねる。
@pytest.fixture
def realistic_payload_vi() -> str:
    import json

    return json.dumps(
        {
            "locale": "vi",
            "incidents": [
                _incident(
                    id="inc-2026-0626-01",
                    location="Aisle 7 (household goods)",
                    type="shelf bracket collapse, stock fell",
                    description="A shelf bracket gave way and boxes fell into the aisle",
                    injury=True,
                    people_affected=1,
                ),
                _incident(
                    id="inc-2026-0626-02",
                    location="Main entrance",
                    type="wet floor after rain, slip risk",
                    description="Rainwater carried in on trolleys; no mat in place",
                    people_affected=8,
                ),
            ],
        },
        ensure_ascii=False,
    )


# ハーネスが 1 回転で使う入力。中核業務（複数重大度 + 履歴あり）を走らせる。
@pytest.fixture
def case_payload(realistic_payload_ja: str) -> str:
    return realistic_payload_ja
