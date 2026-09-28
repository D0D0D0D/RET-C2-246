# Test Specification — RET-C2-246

## Test Strategy
- カバレッジ目標: 主要ロジック（nodes / services）100%、グラフ統合は正常系・異常系・S-1 拒否を網羅
- テスト種別: Unit（nodes / severity_service）/ Integration（5ノード backbone を invoke で通す）/ Proof-of-Boundary
- LLM は注入シーム（`src/services/llm_client.py`）越しに `FakeLLMClient`（`tests/conftest.py`）を注入。実プロバイダ・credential 不要

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict | Type check pass, no Pydantic/dataclass | ✅ PB-2 (`test_state_safety`) |
| TC-02 | SecurityViolationError fires on invalid input | Error raised | ✅ `test_pre_process_node`（不正JSON/必須欠落→status ERROR） |
| TC-03 | No JWT/Credential in State | CI の資格情報スキャン: 0 violations | ✅ keys は注入シーム経由・State 非格納 |
| TC-04 | InvocationContext via configurable only | Direct access raises error | ✅ ctx は invoke 引数・State 非格納 |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | `node_start`/`complete`/`error` absent from `execute()` | ✅ ドメインイベントのみ emit |
| TC-06 | S-2: `_security_gate_input()` not overridden | `@final` 維持（`_extra_*` のみ拡張） | ✅ `_extra_security_gate_input` のみ |
| TC-07 | S-3: `_security_gate_output()` not overridden | `@final` 維持（`_extra_*` のみ拡張） | ✅ `_extra_security_gate_output` のみ |
| TC-08 | `required_trust_level` enforced | Insufficient trust → refused | ✅ `test_s1_trust_denied_for_anonymous` |
| TC-09 | S-2: `_extra_security_gate_input()` non-trivial | 入力サイズ上限を検査 | ✅ `test_oversized_input_rejected` |
| TC-10 | S-3: `_extra_security_gate_output()` non-trivial | PII（email/phone/customer-id）除去 | ✅ `test_scrubs_email_phone_customer_id` |
| TC-11 | S-4: ≥1 domain `emit_trace_event()` per `execute()` | 各ノードでドメインイベント emit | ✅ ingested/classified/severity_scored/content_generated/log_built |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Result |
|-------|----------|------|----------------|--------|
| PB-2 | State serialization | Post-invoke State is primitives only | No Pydantic/dataclass | ✅ `test_state_safety` |
| PB-4 | Import isolation | No Level 0 imports | AST scan: 0 violations | ✅ `test_import_isolation` |
| PB-6 | Invoke execution order | S-1 → S-4 start → S-2 → execute → S-3 → S-4 complete | Order verified | ✅ framework `__call__`（`test_full_backbone_success` の node_history で確認） |

## Business Logic Tests

| TC-ID | Test | Input | Expected Result | Result |
|-------|------|-------|----------------|--------|
| BL-01 | 分類: キーワード→canonical category | "wet floor" / "fire hazard" / 日本語 | slip_zone / fire_hazard 等 | ✅ `test_severity_service` |
| BL-02 | 深刻度採点: 決定論・再現性 | 同一インシデント2回 | 同一スコア | ✅ `test_deterministic_same_input_same_output` |
| BL-03 | 採点: injury/crowd で上昇・10上限 | fire + injury | score 10 / high | ✅ `test_fire_with_injury_is_high_and_capped` |
| BL-04 | トレンド: 履歴有無で出力切替 | historical_incidents 有/無 | pattern_trend 非空/空 | ✅ `test_trend_*` |
| BL-05 | OBS-1: 上流エラーパススルー | status=ERROR | main は `{}` を返す | ✅ `test_obs1_upstream_error_passthrough` |
| BL-06 | 未注入 LLM は fail-closed | MainNode() 既定 | NotImplementedError | ✅ `test_unconfigured_llm_fails_closed` |
| BL-07 | incident_log は集計のみ（生PIIなし） | 通常入力 | description/reporter_note を含まない | ✅ `test_incident_log_has_no_raw_pii_fields` |
| BL-08 | 統合: 5ノード正常系 | structured payload | status success / output.ok | ✅ `test_full_backbone_success` |
| BL-09 | 統合: 不正入力→error route | incidents 空 | status error | ✅ `test_invalid_input_routes_to_error` |

## 実行方法

```bash
python -m pytest tests/ -v
python -m pytest tests/proof_of_boundary/ -v
```

CI（`run-tests`）でも上記2コマンドを実行。ローカル検証時は `agenticstar-agentcore==1.0.0` を
`--extra-index-url`（project 1575 registry）から install してから実行する。

## Test Execution Summary
- Execution date: 2026-06-25（ローカル venv, Python 3.11）
- Total tests: 146（unit/integration 140 + proof_of_boundary 6）
- Pass: 144 / Fail: 0 / Skip: 2（PB-5 checkpoint 無効・PB-7 非 HITL の条件付き auto-waive）
- Coverage: 主要 nodes / services / graph 統合パスを網羅

<!-- LIVE-LLM:BEGIN (generated — 手書きはこのブロックの外へ) -->
## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-29 |
| 使用 LLM | Azure OpenAI の実モデルを `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | ['alert', 'advisory', 'trend', 'incident_log', 'ok'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（外部 CLI と API 鍵が要るため）。
<!-- LIVE-LLM:END -->
モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-28 |
| 使用 LLM | Azure OpenAI の実モデルを `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | ['alert', 'advisory', 'trend', 'incident_log', 'ok'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（外部 CLI と API 鍵が要るため）。

> ⚠️ 上の表は `work/scripts/record_live_llm_in_docs03.py` が**セクションごと再生成**する。
> 以下の節は手書きなので、同スクリプトを再実行したらこの節を書き戻すこと（2026-08-28 に一度消した）。

### 実入力での確認（汎用ペイロードでは出ない欠陥を出すため）

汎用ペイロードの PASS は「壊れていない」ことすら証明しないため、本 AG 本来の業務入力で 2 本回した。
入力は `tests/conftest.py` の `realistic_payload_ja` / `realistic_payload_vi` に残してある
（`case_payload` の向き先を差し替えて実行する）。

| 入力 | 内容 | 結果 |
|---|---|---|
| `realistic_payload_ja` | 4 件（発煙・非常口閉塞・床濡れ・レジ混雑／高3・中1）＋ `historical_incidents` 3 件 | `status=success`・縮退なし。高重大度 3 件が全て場所付きで `alert` に出た。`trend` は入力の履歴日付のみを引用し捏造なし |
| `realistic_payload_vi` | 2 件（棚崩落・雨天の床濡れ／高2）・履歴なし・`locale: vi` | `status=success`・縮退なし。`alert` / `advisory` ともベトナム語で、高重大度 2 件が場所付きで出た。`trend` は履歴なしのため空（決定論強制と一致） |

**この実入力で見つけた欠陥と是正**

- 🔴 **非 ja ロケールで非拘束 disclaimer が読者の言語で出ていなかった。** vi の `advisory` 本文はベトナム語なのに、
  決定論で付与する固定 disclaimer は ja/en のみで、ベトナム語話者には法的注記が読めない言語でしか届かなかった。
  → プロンプトに「`advisory` の末尾に、要求ロケールで同趣旨の一文を置く」指示を追加。固定 ja/en disclaimer は
  権威ある backstop として従来どおり決定論で付与し続ける（保証できるのはこちらだけ）。再実行でベトナム語の
  注記（`Đây không phải là kết luận pháp lý; …`）が出ることを確認した。

**未是正（本改版のスコープ外・報告のみ）**

- `severity_service.classify_type()` はキーワードを dict 順に評価するため、「混雑による通路の狭窄」が
  `overcrowding`（weight 2）ではなく `egress_obstruction`（weight 4）に binning され、`people_affected=30` の
  乗数と合わせて `high` になる。方向は安全側（過剰エスカレーション）だが、非常口閉塞とレジ混雑が同一カテゴリに
  混ざる。決定論分類器の既存挙動であり、本改版（LLM 配線）では触れていない。

### S-3 保全検証バリアントの実 LLM 検証（2026-08-29）

高重大度インシデントが生成文面から落ちていないかの決定論チェックを、**実 LLM の生の応答**に対して検証した。

| 条件 | 結果 |
|---|---|
| `realistic_payload_ja`（高3・中1・履歴あり）実 LLM 応答 | `degradation_reason` 空。高重大度 3 件（`1F…` / `3F…` / `B1…`）すべてアンカー照合で verified。**誤検知なし** |
| `realistic_payload_vi`（高2・履歴なし）実 LLM 応答 | `degradation_reason` 空。`Aisle 7 (household goods)` は翻訳後 `Lối đi 7` でもアンカー `7` で verified。`Main entrance` は `Lối vào chính` となりアンカー不在＝`unverifiable`（違反にしない）。**誤検知なし** |
| 同じ実 LLM 応答から `7` のアンカーだけを削除した改変版 | `degradation_reason: high_severity_omitted` で発火し、決定論文面へ縮退。**アラート自体は出力され**、`Aisle 7` を含む |

変異検査は本改版全体で **54 件すべて赤化**（LLM 配線 27 件 + 保全ゲート 27 件）。パッチがディスクに
反映されたことと復元できたことを毎回 assert するハーネスで測っている（旧ハーネスが 1 件を誤って
SURVIVED と報告したため、数値はこの自己検証版のものが正）。

フレームワーク wheel は **1.0.0 と 1.0.1 の両方**でテスト全通過を確認（1.0.1 は S-3 が入れ子まで再帰するため
挙動差が出うる）。

**敵対レビュー（再レビュー）で実測・是正した欠陥**

| 欠陥 | 実測された挙動 | 是正 |
|---|---|---|
| アンカーが自分自身に一致しない | `MEZZANINE2` → アンカー `EZZANINE2` → 境界照合で自分の場所にも一致せず「欠落」判定 → 決定論文面も同じ理由で落ち hook が raise → **`status: error`・アラート皆無**（検知済みの火災に対して） | アンカーを**最長英数字ラン**から取る。反射性（アンカーは必ず自分の場所に一致）を `TestAnchorReflexivity` で固定 |
| 注入クライアントが正本を破壊できる | duck-typed クライアントが `severity_scores.clear()` / `classified_incidents.clear()` を呼ぶと、State の実体が空になり `incident_log.incident_count: 0`（緩和材料そのものが消える）・`All clear` が無縮退で通る | クライアントには `copy.deepcopy` を渡す |
| 大小・全角半角の差が「欠落」になる | 場所 `b1` と文面 `B1`、`通路７` と `通路7` が不一致＝正当なアラートを誤って縮退 | 照合前に NFKC + casefold 正規化 |
| アンカーを持たない場所は**検査ゼロ** | `Main entrance`（翻訳後 `Lối vào chính`）は照合不能のため、`All clear` でも素通り | 照合不能の高重大度には**決定論の名簿を追記**（`build_high_severity_roster`）。「照合できない」を「照合済み」にしない |

**残る限界（既知・未対応）**

- **照合そのものは意味的な否定を検出できない**（`All clear at Aisle 7` はアンカーを含むため verified）。
  この限界は**決定論名簿の無条件追記で塞いだ** — 高重大度の事実は文面の出来に関わらず必ず出力される。
  残るのは「文面の記述が意味的に忠実か」を*証明*できないことだけで、それには per-incident の構造化出力が要る。
- 単一数字アンカー（`7` 等）は無関係な数値と偶然一致しうる。境界照合で `17` は除外するが偽の「検証済み」は残る。
- `Aisles 3-5` のような範囲表記はアンカー `3` / `5` のいずれか一方の一致で満たされる（`any` 判定）。
  `all` にすると翻訳で片方が落ちた正当な文面を弾くため、可用性を優先している。
- ASCII 以外の数字体系（`قاعة ٧` 等）はアンカーを持たず常に照合不能 → 名簿追記の経路に載る。

**2 巡目の敵対レビューで実測・是正した欠陥**

| 欠陥 | 実測 | 是正 |
|---|---|---|
| 反射性が 64 文字超で再び破れる | `"A"*64 + "1"` は `{1,64}` に分割され末尾チャンク `1` が英数字に前接 → 自分の場所にも一致せず → **`status: error`・アラート皆無**。NFKC 展開（`ﬃ`）で短い入力でも発生 | 反射性フィルタを**復活**（削除は誤りだった）。60〜71 文字の境界を property 的にテスト |
| 意味的否定が素通り | `All clear at Aisle 7` が verified | 決定論名簿を**全高重大度に無条件追記**（照合は縮退の信号として残す） |
| 名簿の行を偽造できる | 改行入りの場所名で `- HIGH … @ ALL CLEAR` の偽行を注入可能 | `render_location()` で制御文字を除去（切り詰めはしない） |
- `severity_service.classify_type()` の先取り binning（上記）は本改版では未対応。

