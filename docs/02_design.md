# Template Design Specification — RET-C2-246

## Position in AgentCore Architecture

- **Agent Class**: RETInStoreSafetyAlertAgent
- **L1 Base**: AgentBaseGraph （← AutonomousBaseGraph ではなくこちらを採用）
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible)
  - Node: L1 inheritance (Template Method: `execute(self, state: dict) -> dict` override only)
  - Graph: composition (`register_nodes()` for node substitution)

> **設計方針**: 本テンプレートは AgentCore SDK v1.0 の固定 5 ノード backbone
> (`initialize → pre_process → main → post_process → finalize`) を採用する Cat 2 ChatAgent。
> 旧 Level 2 base 継承モデルは廃止され、L1 の `AgentBaseGraph` を直接継承する（ChatAgent は概念パターン参照）。
> 設計上の 6 論理ステップを 3 つのドメインスロット（pre_process / main / post_process）に集約する。

> **永続化方針（ステートレス・入力駆動 / Decision 採用）**: 本エージェントは **1 回の呼び出しで完結するステートレスな生成器**であり、
> 業務 DB への書き込み・インシデント履歴の蓄積は**行わない**。場所別インシデントの蓄積・分析は**上流（店舗のインシデント管理側）の責務**とする。
> 「場所ごとの危険性トレンド（historical pattern trend）」が必要な場合は、**呼び出し側が直近の過去インシデントを入力として渡し**
> （`historical_incidents`）、本エージェントはそれを材料にトレンドを**生成**する。これにより顧客インフラへの密結合を避け、
> どの店舗にも落とせる ready-made 性を保つ（カタログの既存 Cat2 テンプレと同じステートレス方針）。

## Architecture Overview

### 6 論理ステップ → 3 ドメインスロットのマッピング

提案時の論理ノードフロー
`IncidentDataIngest → AnomalyClassify → SeverityScore → AlertGenerate → ReportingAdvisory → IncidentLogOutput`
を、固定 backbone の 3 スロットへ次のように畳み込む。

| 論理ステップ | 担当スロット | 役割 |
|---|---|---|
| 1. IncidentDataIngest | pre_process | 構造化インシデント＋スタッフ報告テキストの正規化・バリデーション |
| 2. AnomalyClassify | pre_process | 場所ごとに事故/異常の種別を分類（危険性の地図化） |
| 3. SeverityScore | main | **決定論ルール**で深刻度を採点（再現性確保。LLM を使わない） |
| 4. AlertGenerate | main | LLM が config locale で現場非専門家向けの安全啓発アラートをネイティブ生成 |
| 5. ReportingAdvisory | main | 通報を促す advisory を生成（「報告義務を断定しない」をプロンプトで統制） |
| 6. IncidentLogOutput | post_process | `S-3 OutputGate` で PII を除去し、インシデントログを整形して出力 |

### Node Configuration

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | schema_version / session_id / trust_level 設定 | — | — | InitializeNode (default) |
| pre_process | インシデント正規化＋種別分類（IncidentDataIngest + AnomalyClassify） | `user_input`（JSON 文字列）、`input_context.locale` | `validated_input`, `normalized_incidents`, `classified_incidents`, `historical_incidents`, `locale`, `status` | FunctionNode override |
| main | 決定論採点＋LLM 安全啓発生成＋通報 advisory＋（任意）トレンド生成（SeverityScore + AlertGenerate + ReportingAdvisory + PatternTrend） | `status`, `classified_incidents`, `historical_incidents`, `locale` | `severity_scores`, `safety_alert`, `reporting_advisory`, `pattern_trend`, `degradation_reason`, `status` | FunctionNode override |
| post_process | PII 除去・ログ整形（IncidentLogOutput, S-3 OutputGate） | `status`, `classified_incidents`, `severity_scores`, `locale`, `safety_alert`, `reporting_advisory`, `pattern_trend`, `degradation_reason` | `incident_log`, `result`, `status` | FunctionNode override |
| finalize | response_metadata / total_time_ms 構築 | — | — | FinalizeNode (default) |

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max 3)
                                          pre_process
```

- `pre_process → main` は無条件辺。`route()` は **main の後**に `state["status"]` を読み、
  `success → post_process` / `retry → pre_process` / それ以外 → finalize へ分岐する（`route()` は override しない）。
- 上流が `status="error"`（`AgentStatus.ERROR`）を立てた場合、main / post_process は処理をパススルーする。

### State Definition

`State(AgentState)` に以下のフィールドを追加する（共有フィールド `user_input` / `status` /
`session_id` / `node_history` / `error_log` / `validated_input` / `input_context` 等は `AgentState` から継承）。

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `historical_incidents` | `list[dict]` | **入力（任意）**: 呼び出し側が渡す過去インシデント。トレンド生成の材料（蓄積は上流の責務、本AGは溜めない） | No |
| `normalized_incidents` | `list[dict]` | 正規化済みインシデント一覧（場所・種別・時刻等） | Yes |
| `classified_incidents` | `list[dict]` | 事故/異常の種別が付与されたインシデント | Yes |
| `severity_scores` | `list[dict]` | インシデントごとの決定論的深刻度スコア | Yes |
| `safety_alert` | `str` | config locale で生成された安全啓発アラート本文 | Yes |
| `reporting_advisory` | `str` | 通報を促す advisory（断定しない助言） | Yes |
| `pattern_trend` | `str` | **出力**: `historical_incidents` が渡された場合の場所別危険性トレンド要約（無ければ空） | No |
| `incident_log` | `dict` | PII 除去後のインシデントログ（出力成果物） | Yes |
| `result` | `dict` | 最終レスポンス（アラート＋advisory＋trend の要約。AgentState 標準規約に従い `dict`） | Yes |

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types)
- No JWT, API keys, credentials in State (checkpoint DB leakage)
- InvocationContext via `config["configurable"]` only (not in State)
- No Pydantic models, dataclass, arbitrary Python objects (msgpack incompatible)
- 個人を特定し得るインシデント記述は `incident_log` 化の段階で OutputGate により除去する。

### AGENTIC STAR Marketplace entry adapter

ルートの `cli.py` は Marketplace runner の汎用 adapter を置き換える。runner が渡す
`input_context.conversation_history` は S-2 が走査する `user_input` / `validated_input` / `llm_response`
のいずれでもないため State へマージしない。adapter は `input_context` を破棄し、adapter 所有の
`{"locale": config.locale}`（未指定時 `ja`）だけを組み直す。したがって自由文やそこに含まれ得る
従業員・顧客の識別情報について、新しい State 流入経路は作らない。payload 自身の `locale` があれば
pre_process の既存優先順位により config 既定値を上書きする。

Marketplace 固有境界の適用判断は次のとおり。

- **scope: N/A** — この AG に scope モデルも scope consumer もない。`src/api/server.py` の記述どおり、
  “this template has NO scope model. ... Enforcing authenticate_scope() would gate on a scope nothing consumes
  (STG-default theater)” であるため、利用されない tenant/org 環境変数や起動 gate を新設しない。
- **書込モード: N/A** — `mode` フィールドおよび ingest / feedback / upsert 経路がなく、1 invocation で
  完結する読み取り・生成処理なので、Marketplace adapter に死んだモード抑止を置かない。
- **HITL: N/A** — `config.yaml` に `hitl` 設定がなく `interrupt()` も呼ばないため、context の
  `hitl_allowed` を書き換えない。

Azure 接続値は allowlist provider が `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` /
`AZURE_OPENAI_DEPLOYMENT` のみを公開し、`provision_secrets()` で欠落・空白・改行を起動時に拒否する。
Azure client はキャッシュせず、各 `complete()` / `stream()` で `current_secrets()` から接続値を引いて
構築する。Marketplace runner は compile 後に secrets を provision するため、接続値を保持しない
`AzureLLM` wrapper を graph 登録時に既存の `config["llm"]` seam へ載せ、provision 時に allowlist
provider を基底 graph へ束縛する。

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (correlation_id, session_id, permissions, credential handle)
- [x] ConnectionPolicy (retry/timeout strategy) — main の LLM 呼び出しに適用（max_retry=3）
- [x] SecurityViolationError
- [x] S-2: `_extra_security_gate_input()` — 入力インシデント JSON のサイズ上限・必須フィールド
      （location / type）検証を追加実装する。デフォルトの PII スキャン（`user_input` / `validated_input`）の後段で動作。
      **`_security_gate_input()` は override しない**（クラス定義時 `TypeError`）。
- [x] S-3: `_extra_security_gate_output()` — `safety_alert` / `reporting_advisory` / `incident_log` に対する
      PII 再スキャン（氏名・連絡先・顧客識別子の除去確認）を追加実装する。デフォルトの credential scan の後段で動作。
      **`_security_gate_output()` は override しない**（クラス定義時 `TypeError`）。
- [x] S-4: `emit_trace_event()` — 各 `execute()` 内でドメインイベントを最低 1 件 emit
      （例: `incident_classified` / `severity_scored` / `alert_generated`）。
      `node_start` / `node_complete` / `node_error` は `BaseNode.__call__()` が自動 emit するため重複 emit しない。

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `FunctionNode` subclass → framework `@final` gate always runs automatically;
>   extend via `_extra_security_gate_input()` / `_extra_security_gate_output()` only
> - `GraphNode` / `RemoteAgentNode` → deliberate no-op (upstream or remote node's gate already applied)
> - Custom `BaseNode` subclass → must implement `_security_gate_input()` and
>   `_security_gate_output()` directly (`@abstractmethod` — omission raises `TypeError` at instantiation)
>
> 本テンプレートの 3 スロットはすべて `FunctionNode` サブクラスなので、`@final` ゲートが自動実行され、
> ドメイン固有チェックは `_extra_*` フックで追加する。PII 除去という核心要件は post_process の
> `_extra_security_gate_output()` で担保する。

### Security Model (S-1 〜 S-5)

本テンプレートの 5 層セキュリティの実装箇所・適用タイミングを明示する（framework の `S-1`〜`S-5` 定義に準拠）。

| Layer | 名称 | 実装箇所 | 適用タイミング | 本AGでの内容 |
|-------|------|---------|---------------|-------------|
| **S-1** | Trust Gate | `BaseNode.__call__()`（override 禁止） | 各ノードの `execute()` 実行**前** | `required_trust_level = VERIFIED_EXTERNAL`（config/agent.yaml）。会話/標準操作の最小トラスト。 |
| **S-2** | Input Security Gate | `FunctionNode._security_gate_input()`（`@final`）+ `_extra_security_gate_input()` | pre_process の `execute()` 前 | デフォルト PII スキャンに加え、入力インシデント JSON のサイズ上限・必須フィールド（location / type）検証を `_extra_*` で追加。 |
| **S-3** | Output Security Gate | `FunctionNode._security_gate_output()`（`@final`）+ `_extra_security_gate_output()` | post_process（IncidentLogOutput）の `execute()` 後 | デフォルト credential scan に加え、`safety_alert` / `reporting_advisory` / `incident_log` の **PII 再除去**（氏名・連絡先・顧客識別子）を `_extra_*` で担保。 |
| **S-4** | Audit Logging | `shared.utils.audit_logger.emit_trace_event()` | 各 `execute()` 内（side-effect 操作ごと） | `incident_classified` / `severity_scored` / `alert_generated` 等のドメインイベントを emit。`node_start/complete/error` は framework が自動 emit するため重複させない。 |
| **S-5** | Credential Scan / Supply Chain | CI の資格情報スキャン + 依存ピン留め検査 | push / MR パイプライン時 | `src/` に credential を置かない（テストは `"mock-..."` ラベル値）。`[project].dependencies` は `==` 完全ピン。 |

> **S-5 補足（reviewer 指摘 F-04 への回答）**: 本 framework の **S-5 は「credential scan / supply-chain 供給網（CI ゲート）」であり、
> 通報の "ディスクレーマー" 層ではない**。通報を断定しない安全ガードレール（「報告義務を断定しない・適用法令/所管は人間が確認」）は
> **S-5 ではなくドメインのプロンプトガードレール**として main（ReportingAdvisory）で担保する（Design Decision Record「通報判断」参照）。
> S-5 はコード/State に秘匿情報を載せず依存を固定する CI 強制で満たす。

### Composition Pattern

- **Pattern**: Standalone（3 スロットを単一 `AgentBaseGraph` に register。GraphNode subgraph は使わない）
- **Composition target**: N/A（線形 3 スロットで完結。内側ドメイングラフ不要）
- **Error propagation strategy**: propagate（pre_process が `AgentStatus.ERROR` を返したら route が finalize へ）

> **備考（Cat 2 の composition 選択）**: 提案の 6 論理ステップは線形であり autonomous loop も
> 分岐ワークフローも要しないため、内側 `domain_workflow_graph.py`（GraphNode）には展開せず、
> 3 スロットへ直接畳み込む Standalone 構成とする。これにより実装・テストが最小化される。

### S-3 保全検証バリアント（高重大度の文面保全）

`severity_service` が確定した **`severity == "high"` のインシデントが生成 `alert` から落ちていないか**を
決定論的に検証する。S-3 の *Preservation Variant*（出力が安全上重要な内容を欠いていたら弾く形）。

**2 段構え** — 文字列照合だけでは保証を運べないため、役割を分ける。

| 段 | 場所 | 役割 |
|---|---|---|
| **信号**（検証＋是正） | `MainNode.execute()` | **モデルの文面**を照合し、高重大度が落ちていたら決定論文面へ**差し替え**て `degradation_reason: high_severity_omitted` を立てる。変換は必ずここで行う（hook は文面を作り直せないため、hook で書き換えると監査イベントと実出力が乖離する） |
| **保証**（無条件の追記） | `MainNode.execute()` | 非縮退時、**全高重大度インシデントの決定論名簿**を `alert` 末尾に追記する。順序は「信号 → 保証」（逆にすると自分で追記した名簿が照合を満たして検査が空になる） |
| 最終防波堤 | `MainNode._extra_security_gate_output()` | `execute()` のバグで未検証の文面が通ったら `RuntimeError` で止める。**検証のみ・変換しない**。`severity_scores` / `safety_alert` の**欠落は異常として fail closed**（`.get(key, [])` で既定値を与えると「ゼロ件」と「欠落」が同一視され fail open になる） |

**なぜ照合だけでは足りないか（保証を別立てにした理由）**

トークン照合が答えられるのは「その場所名が文面に出るか」だけで、実測した 2 つのケースを取りこぼす:
- **アンカーを持たない場所**（`Main entrance` → `Lối vào chính` で共通部分ゼロ）は**判定不能**
- **場所を挙げつつ否定する文面**（`All clear at Aisle 7`）はアンカーを含むので**素通り**

決定論名簿の追記はこの両方を言語非依存に塞ぐ。照合は「モデルの文面が落とした」ことを検知して縮退させる**信号**として残す。

**照合方式と既知の限界**

- 場所名から**識別子アンカー**（数字を含むトークン: `B1` / `3F` / `7`）を抽出し、英数字境界付きで `alert` を検索する。
- **アンカーを持たない場所は `unverifiable`**（例: `Main entrance`）。翻訳後は共通部分が残らないため照合できない。
  **検証済みとして扱わず、違反としても扱わない**（違反にすると正当なアラートを止める）。監査イベントに件数を出す。
- 単一数字アンカー（`7` など）は文面中の無関係な数値と偶然一致しうる。境界照合で `17` 等は除外しているが、
  **偽の「検証済み」は原理的に残る**。
- **アンカー抽出には反射性フィルタが必須**（`_anchor_in(a, normalized)` で自分自身に一致しないアンカーを捨てる）。
  `{1,64}` は最長ランを取るのではなく**長いランを分割する**ため、65 文字以上の英数字ランは末尾チャンクが
  英数字に前接して自分の場所にも一致しなくなる。フィルタが無いと「欠落」判定 → 決定論文面も同じ理由で落ち →
  backstop が raise → **`status: error`・アラート皆無**（検知済みハザードに対して）。NFKC 展開（`ﬃ` → 3 文字）で
  64 文字未満の入力でも起きうる。捨てられたアンカーは `unverifiable` 経路＝名簿保証に載る（安全側）。
- 単一数字アンカー（`7` など）は文面中の無関係な数値と偶然一致しうる。境界照合で `17` 等は除外しているが、
  **偽の「検証済み」は原理的に残る**。`All clear at Aisle 7` のような**意味的な否定も照合では検出できない** —
  だからこそ名簿追記を無条件にしている。
- 名簿・決定論文面に載せる場所名は `render_location()` で**制御文字を除去**する。呼び出し側が改行を含む場所名で
  `- HIGH … @ ALL CLEAR` のような**決定論に見える偽の行**を注入できるため。切り詰めはしない（アンカーが消えると
  追記した名簿自身が照合に落ちる）。
- 注入クライアントには `copy.deepcopy` を渡す。正本のリストを参照で渡すと、duck-typed クライアントが
  `clear()` して検査対象と `incident_log` を同時に消せる。
- 監査イベントと例外メッセージには**件数のみ**を載せる（場所は呼び出し側の自由文のため）。

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (no agents/base/ required)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 固定多段ワークフローであり自律 think→act ループ不要。ChatAgent パターンは AgentBaseGraph 系。 |
| Composition pattern | Standalone (3 slots) | GraphNode subgraph | **Standalone** | 6 論理ステップは線形で分岐なし。内側グラフ展開は過剰。3 スロット集約で十分。 |
| Severity scoring | 決定論ルール | LLM 採点 | **決定論ルール** | 再現性・監査性を確保。安全領域で出力ブレを避ける。LLM は啓発文生成のみに限定。 |
| 多言語出力 | 専用翻訳機構 | config locale で LLM ネイティブ生成 | **config locale** | 安全用語辞書/専用機構は moat を持たず誤訳リスク増。翻訳は汎用別レイヤーへ委譲。 |
| 通報判断 | 固定所管マッピング config | プロンプトで advisory（断定しない） | **advisory** | 法令/所管は変動し誤りが致命的。「報告義務を断定しない・人間が確認」を強制する助言に留める。 |
| インシデント永続化 | AG内でDBに蓄積 | ステートレス（蓄積は上流・トレンドは入力駆動） | **ステートレス** | 単一責任を保ち顧客インフラへの密結合を回避。履歴は呼び出し側が `historical_incidents` で渡す。既存 Cat2 テンプレと同方針。 |
| LLM 注入シーム | Graph/ノードの独自コンストラクタ引数 | `config["llm"]`（同系テンプレート共通） | **`config["llm"]`** | `BaseGraph.__init__(self, config=None)` が framework の実契約。独自引数は呼び出し側が渡さないため永久に未配線になる。2026-08-28 まで実際にその状態だった。 |
| LLM プロトコル差の吸収 | テスト側にアダプタ | `src/services/llm_client.py` にアダプタ | **src 側** | 注入されるのは `BaseLLM.complete(messages)`、本 AG のノードが呼ぶのは `generate_safety_content(...)`。アダプタがテストにしかないと本番経路にアダプタが存在しない。 |
| LLM 未注入時 | 決定論定型文へ縮退 | 失敗（`status: error`） | **失敗** | 未注入はデプロイの設定漏れ。静かに縮退させると安全 AG が恒久的に機械文だけを出し続け誰も気づかない。develop の挙動も同じ。 |
| LLM 実行時失敗時 | 失敗（`status: error`） | 決定論定型文へ縮退（`degradation_reason` 付き） | **縮退** | 検知・重大度は決定論層で確定済み。プロバイダの一時障害で火災ハザードの通知そのものが消える方が危険。縮退文面は決定論の事実（場所/カテゴリ/重大度）だけを機械的に列挙し、任意ロケールの流暢文は作らない。 |
| 通報 advisory の非拘束表現 | プロンプト指示のみ | 決定論で固定 disclaimer を必ず付与 | **決定論付与** | 「法的判断ではない」はプロンプト遵守に依存させない。固定文は ja/en。要求ロケールでの同趣旨文はプロンプトでも依頼するが、そちらはモデル記述のため保証しない。 |
| 履歴なし時の trend | プロンプト指示のみ | 決定論で空文字に強制 | **決定論強制** | 根拠データが無いのにトレンドを主張させない。`finalize_safety_content()` がクライアント種別によらず適用する。 |
| 高重大度の文面への保全 | プロンプト指示のみ | S-3 保全検証バリアントで検証 | **保全検証** | 高重大度の事象が生成文面から落ちると「検知されなかった」ように読める。S-3 が認める保全検証バリアント（フィルタでなく欠落の検証）を `_extra_security_gate_output()` に置く。判定の正本は決定論の `severity_scores` で、モデルの重大度判断は参照しない。 |
| 保全検証に落ちた時 | 出力を止める（error） | 決定論文面へ縮退（success） | **縮退** | 安全 AG では「アラートが出ない」コストも高い。全ハザードを名指しする決定論文面を出す方が、遮断より安全。`degradation_reason: high_severity_omitted` で機械判別可能にする。 |
| 多言語での場所照合 | 逐語一致 | 識別子アンカー（数字を含むトークン）＋照合不能の 3 値 | **アンカー＋3 値** | 逐語一致は誤検知で正当なアラートを止める（実測: vi で `Aisle 7` → `Lối đi 7`）。翻訳を越えて残るのは階数・通路番号などの識別子。アンカーが無い場所（`Main entrance` → `Lối vào chính`）は **`unverifiable`** として記録し、**検証済みにも違反にもしない**。 |
