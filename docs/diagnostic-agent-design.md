# VoxBench 診断エージェント設計・実装計画 v0.1

> Durable decisions and implementation order are summarized in [`MEMORY.md`](../MEMORY.md).

## 1. 結論

VoxBench に追加するべきものは、汎用チャットボットではなく、選択中の通話を起点に、VoxBench が保持する観測証拠と、許可された外部証拠を安全に調査する「診断セッション」である。

最初のリリースは読み取り専用に限定する。LLM に SQL、ファイルシステム、シェル、PCAP、録音、Git リポジトリを直接渡さず、Control Plane が提供する型付き・制限付きの診断ツールだけを呼ばせる。数値計算、相関、ルール判定、秘匿化は決定論的なコードで行い、LLM は調査計画、ツール選択、仮説比較、説明を担当する。

最初に実現する利用体験は次の通り。

1. Call inspector で対象 run を選ぶ。
2. 右側の「Ask VoxBench」を開く。現在の run、選択 incident、cursor 時刻が自動で文脈に入る。
3. 「この切断の原因は？」と質問する。
4. エージェントが evidence coverage、incident、SIP/RTP、pipeline、provider、runtime、config を順に調査する。
5. 回答を「観測事実」「推定」「代替仮説」「不足している証拠」「次の安全な実験」に分ける。
6. エージェントが「この区間を見てください」と Call inspector の cursor を移動し、該当 lane と evidence を強調し、必要なら録音をその位置から再生する。

診断レポートと guided investigation は同じ evidence ref を使う。文章の引用を押して人が移動するだけでなく、エージェント自身が安全な UI command を提示・実行できることを製品要件に含める。

## 2. 現状理解

### 2.1 既存の強み

VoxBench はすでに診断エージェントの土台を持つ。

- `run_id` を中心に config、SIP、RTP、stage recording、metric、span、provider event、host state を相関する。
- timeline は `events`、`intervals`、`series`、`artifacts`、`incidents` の型付き projection を返す。
- incident は `observed`、`expected`、`confidence`、`evidence_refs` を持つ。
- clock domain と `alignment_uncertainty_ms` を保持し、異なる時計の精密な一致を断定しない。
- raw provider ID、秘密、URL、Slack ID、通信相手の生情報を safe alias に置き換える設計がある。
- 音声、RTP packet cadence、capture health、barge-in、dead air、playback gap、level/duration などの決定論的検出がある。
- Postgres 永続化、job lease、fenced commit、MinIO 録音、短命 audio session の境界がある。

したがって、エージェントが最初に読むべき正規の情報源は raw log ではなく、既存の typed timeline と incident である。

### 2.2 現状の不足

- 診断ルールと API projection の多くが `run_api.py` に集中しており、ツールとして再利用しづらい。
- Web UI の主要機能が `App.tsx` に集中しており、チャットドロワーをそのまま追加すると保守性が悪化する。
- raw PCAP/Wireshark import は未実装。現在は構造化 SIP/RTP ingest と一時的 packet tap が中心。
- 一般的な Asterisk/Pipecat/application log の取り込み、全文検索、行単位引用がない。
- source repository の登録、commit snapshot、コード検索、行単位引用がない。
- transcript は意図的に保持していない。音声内容を診断に使う場合の同意・秘匿・保持方針も未定義。
- 製品全体の operator auth、RBAC、監査、retention/deletion は未完成。
- incident は timeline 生成時に導出される projection であり、診断セッション、仮説、回答、引用は永続化されない。
- production deployment で任意の外部 LLM へどの証拠を送れるかという egress policy がない。

## 3. 設計原則

### 3.1 観測事実と推論を分離する

診断回答の文を、最低でも次の claim type に分類する。

- `observed`: 保存された証拠から直接読める。
- `derived`: 決定論的な計算で導いた。計算方法と入力 evidence を示す。
- `inferred`: 複数証拠からの仮説。confidence と反証条件を持つ。
- `unknown`: 必要な lane または観測境界がない。
- `recommended`: 次の調査・実験。事実とは分ける。

LLM が引用なしの `observed` claim を返すことは禁止する。引用できない場合は `inferred` または `unknown` に落とす。

### 3.2 決定論的診断を先、LLM を後に置く

RTP gap、無送信時間、BYE の時刻差、direction、Q.850 cause、stage 間の尺、RMS、packet count、capture health などは Python の analyzer が計算する。LLM に時刻表や数千 packet を読ませて暗算させない。

### 3.3 read-only by default

v1 のツールは読み取り専用とする。設定変更、実験実行、サーバー操作、コード修正、commit、PR は行わない。将来の mutation は「提案作成」と「実行」を別権限・別API・別監査イベントにする。

### 3.4 証拠の欠落を健康と解釈しない

RTP lane が空なら「RTP は正常」ではなく「RTP は未観測」と答える。caller-side playout を観測していない場合、相手が聞いた音を断定しない。capture drop counter がなければ packet loss の確信度を上げない。

### 3.5 最小開示

モデルに渡すものは質問に必要な safe projection のみとする。録音、transcript、raw log、raw SIP/SDP、PCAP payload、resolved config 全体、repository source は自動添付しない。

## 4. 提案アーキテクチャ

```text
Call inspector / Ask VoxBench drawer
                |
                | POST message / SSE events
                v
Diagnostic API (FastAPI)
  - session/message persistence
  - authorization and policy
  - streaming event projection
  - UI command validation
                |
                v
Diagnostic Orchestrator
  - bounded state machine
  - tool allowlist / budgets / timeout
  - claim and citation validation
       |                    |
       v                    v
Deterministic analyzers   Model adapter
       |
       v
Evidence services
  - timeline/incident/run/config
  - cross-run comparison
  - logs (future adapter)
  - captures (future adapter)
  - source snapshots (future adapter)
                |
                v
Postgres metadata + object storage artifacts
```

UI操作はLLMがDOM selectorやJavaScriptを生成する方式にしない。VoxBenchが定義した型付きUI commandを、現在開いている画面が解釈する。

```text
Assistant claim / explanation
          |
          v
Typed UI command -> server-side scope validation -> SSE
          |
          v
Web command dispatcher -> UI state action -> client acknowledgement
```

### 4.1 新しい境界

現在の `run_api.py` から、まず以下を抽出する。

- `timeline/models.py`: timeline response と evidence reference の型。
- `timeline/projection.py`: StoredRun から typed timeline を作る純粋関数。
- `diagnostics/rules/`: 既存 incident ルール。rule ID と version を明示する。
- `diagnostics/evidence_service.py`: run と evidence ref を安全に取得する。
- `diagnostics/analyzers/`: 切断、無音、RTP、barge-in、level/duration 等の計算。
- `diagnostic_agent/`: session、orchestrator、tools、model adapter、policy、claim validator。

既存 API response は維持し、内部移動だけを先に行う。診断エージェント実装と同時に大規模なレスポンス変更をしない。

### 4.2 エージェント実行モデル

自由な ReAct loop ではなく、上限付き状態機械を推奨する。

```text
scope -> inspect coverage -> inspect incidents -> gather evidence
      -> compare hypotheses -> validate claims -> answer
```

1リクエストあたりの初期上限例:

- 最大 tool call: 12
- 最大 wall time: 45秒
- 最大 run 数: 5
- 最大 evidence item: 200
- 最大 log excerpt: 20断片、各40行
- 最大 source excerpt: 12断片、各80行
- audio/raw capture のモデル送信: v1では禁止

値は manifest または deployment policy に置き、コアへ診断対象固有の値をハードコードしない。

## 5. 診断ツール契約

v1 では次のツールに絞る。

### 5.1 `get_run_context`

入力: `run_id`

返却:

- safe run metadata
- provider/engine/config hash
- start/end/status
- evidence coverage と missing boundaries
- available incident/rule summary
- related run candidates（同じ config/environment/tag の safe ID）

### 5.2 `list_incidents`

入力: `run_id`, optional category/severity/time range

返却: incident ID、rule/version、時刻、confidence、要約、evidence refs。

### 5.3 `get_evidence`

入力: `run_id`, evidence refs

返却: type discriminated union。event、interval、series window、verification、artifact metadata、safe config excerpt を返す。任意 URI、raw attrs、秘密は返さない。

### 5.4 `analyze_disconnect`

入力: `run_id`

決定論的に返す項目:

- 最終 SIP transaction と BYE direction/status/reason alias
- caller/assistant 両方向の最終 media observation
- BYE までの gap
- RTP/RTCP/CN を区別できたか
- capture health
- caller hangup、upstream timeout、local timeout を識別する証拠の有無
- 断定不能な境界

今回の例では「GW発BYE」だけでは caller hangup と中間PBX timeout を区別できない、と返せることが必須である。

### 5.5 `analyze_media_continuity`

方向別に sequence gap、arrival stall、media advance、digital silence、playback burst、provider response、stage output を相関する。

### 5.6 `compare_runs`

入力: 最大5 run、比較軸。

同じ測定項目だけを比較し、coverage が異なる run を同等に扱わない。設定差分は allowlisted path と hash を返し、secret ref の値は返さない。

### 5.7 `search_known_cases`

最初は embedding/RAG ではなく、incident rule、provider、engine、symptom tag、config hash、environment profile による構造化検索を使う。診断セッションで人が「解決」「誤診」「再現せず」を付けた case のみ knowledge として昇格させる。

## 6. 将来ツール

### 6.1 ログ

`LogSourceAdapter` を定義し、Loki/OpenSearch/file 等を差し替える。ツールは任意 query language を受け取らず、`run_id`、安全な source alias、time range、severity、既知 correlation alias だけを受ける。adapter が server-side query を作る。

log excerpt は ingest 時または返却時に redaction し、行ごとに `log_ref` を発行する。回答は `log_ref` と time range を引用する。

### 6.2 コード

稼働中のコンテナや開発者の working tree を直接検索しない。診断対象 run に `source_snapshot` を関連付ける。

- repository alias
- commit SHA
- dirty/clean/unknown
- deployment artifact digest
- allowlisted paths

`SourceSnapshotAdapter` は read-only checkout/index を検索し、`source_ref`、commit、path、line start/end、excerpt hash を返す。モデルの回答中で path だけを出すのではなく snapshot と行範囲を引用する。

### 6.3 PCAP

raw packet payload をモデルへ渡さない。sandboxed parser が SIP transaction、RTP fixed header、RTCP aggregate、capture health、5-tuple alias を構造化 evidence に変換する。SDP、電話番号、IP は policy に従い alias 化する。

## 7. データモデル

Postgres に以下を追加する。message 本文の保持は deployment policy で無効化または短期化できるようにする。

### 7.1 `diagnostic_sessions`

- `id` UUID
- `primary_run_id` FK
- `status`: active/completed/failed/cancelled
- `created_by_alias`
- `policy_version`
- `model_provider_alias`, `model_alias`
- `created_at`, `updated_at`, `expires_at`

### 7.2 `diagnostic_messages`

- `id`, `session_id`, `ordinal`
- `role`: user/assistant/system_event
- `content_redacted`
- `created_at`
- `finish_reason_alias`

### 7.3 `diagnostic_tool_calls`

- `id`, `session_id`, `message_id`, `ordinal`
- `tool_name`, `tool_version`
- `input_redacted`, `output_summary`
- `started_at`, `ended_at`, `outcome`, `failure_alias`
- `evidence_refs`

### 7.4 `diagnostic_claims`

- `id`, `message_id`, `ordinal`
- `claim_type`
- `text`
- `confidence`
- `evidence_refs`
- `status`: supported/unsupported/partially_supported

### 7.5 `diagnostic_cases`（後続）

- session/run references
- normalized symptom tags
- confirmed/likely/unresolved outcome
- resolution summary
- operator validation
- applicable versions/config hashes

エージェントの自由文をそのまま「過去事例」として検索対象にしない。人の検証済み case を正規化してから利用する。

## 8. API

```text
POST   /diagnostic-sessions
GET    /diagnostic-sessions/{session_id}
POST   /diagnostic-sessions/{session_id}/messages
GET    /diagnostic-sessions/{session_id}/events     # SSE
POST   /diagnostic-sessions/{session_id}/cancel
GET    /diagnostic-sessions/{session_id}/messages
GET    /runs/{run_id}/diagnostic-sessions
```

message POST は非同期 job ID を返し、SSE で次の安全なイベントを流す。

- `status`: 調査段階
- `tool_started`: ツール名と目的（raw input は出さない）
- `evidence_found`: safe evidence ref と要約
- `claim`: 検証済み claim
- `answer_delta`: 表示用本文
- `completed` / `failed`
- `ui_command_proposed`: 実行予定の安全なUI操作
- `ui_command`: scope検証済みのUI操作
- `ui_command_result`: clientの実行結果または拒否理由

再接続可能にするため event ordinal を永続化する。WebSocket は既存 live preview と責務が異なるため流用しない。

### 8.1 UI command contract

初期版で許可する command は、VoxBench 内の表示・再生操作に限定する。

```json
{
  "command_id": "uuid",
  "type": "focus_evidence",
  "run_id": "run-id",
  "evidence_ref": "event:...",
  "presentation": {
    "open_inspector": true,
    "highlight_ms": 3000
  }
}
```

command type:

- `select_run`: Primary/Compare runを選択する。
- `select_incident`: incident drawerを開く。
- `focus_evidence`: timeline cursorを根拠時刻へ移動しlaneを強調する。
- `set_time_window`: 指定区間へzoomする。
- `open_panel`: SIP ladder、RTP、pipeline、provider、host等を開く。
- `play_recording`: 許可済みstage録音を指定位置から再生する。
- `pause_recording`: エージェントが開始した再生を停止する。
- `show_comparison`: 比較runと比較項目を表示する。
- `apply_view_filter`: 表示上のcategory/direction/stageを絞る。
- `start_guided_sequence`: 複数の説明stepを順番に見せる。

任意selector、任意URL遷移、任意JavaScript、フォーム入力、設定保存、API呼び出しはcommandに含めない。

各commandは以下を満たす必要がある。

- sessionが参照を許可されたrun/evidence/artifactだけを対象にする。
- evidence refから時刻・lane・artifactをserver側で解決する。LLMが任意時刻やURIを作らない。
- clientは実行前後の状態と結果をacknowledgeする。
- 対象が現在のtimelineに存在しない場合は安全に失敗する。
- commandは再送されても二重再生等を起こさないようidempotentに扱う。
- 自動再生はブラウザのautoplay制約と利用者設定に従う。

### 8.2 Guided investigation

回答には、静的な文章とは別に検証済みのstep列を含められる。

```json
{
  "title": "23秒の無送信とBYEを確認する",
  "steps": [
    {
      "narration": "発信者側のRTPは継続しています。",
      "command": {"type": "focus_evidence", "evidence_ref": "..."}
    },
    {
      "narration": "AI送出側の最後のmedia observationです。",
      "command": {"type": "focus_evidence", "evidence_ref": "..."}
    },
    {
      "narration": "この後にGW発のBYEがあります。",
      "command": {"type": "select_incident", "incident_id": "..."}
    }
  ]
}
```

利用者は「次へ」「戻る」「停止」で操作する。既定はstepごとのユーザー進行とし、連続自動実行は明示的に開始した場合だけ許可する。

## 9. 回答フォーマット

モデルには最終的に構造化出力を要求する。

```json
{
  "summary": "...",
  "claims": [
    {
      "type": "observed",
      "text": "...",
      "confidence": "certain",
      "evidence_refs": ["event:..."]
    }
  ],
  "hypotheses": [
    {
      "title": "...",
      "confidence": "medium",
      "supporting_refs": [],
      "contradicting_refs": [],
      "disconfirming_test": "..."
    }
  ],
  "missing_evidence": [],
  "recommended_actions": []
}
```

server-side validator が存在しない evidence ref、run scope 外の ref、引用なし observed claim、許可されない action を拒否し、1回だけ修正生成する。それでも不正なら、安全な固定エラーと取得済み証拠一覧を返す。

## 10. UI 設計

### 10.1 配置

常駐の丸いウィジェットより、Call inspector に結びついた右ドロワーを基本とする。狭い画面では full-screen sheet にする。入口は以下の2つ。

- グローバル: `Ask VoxBench`
- incident drawer: `この事象を調べる`

自動 context は明示的な chip として表示し、ユーザーが外せる。

- Run
- Incident
- Cursor ± 任意秒
- Compare run

### 10.2 表示要素

- 調査中の段階と停止ボタン
- 質問・回答
- claim type と confidence
- 根拠 chip。押すと該当 incident、cursor、stage recording に移動
- 「不足している証拠」
- 「次の実験」
- feedback: helpful / incorrect / resolved / needs expert review
- `画面で見る`: claimのevidenceへ移動
- `一緒に確認`: guided investigationを開始
- 実行中step、次に動く画面、停止ボタン

内部の chain-of-thought は表示しない。表示するのはツール実行の目的、取得した証拠、検証済み claim だけである。

エージェントが画面を動かした直後は、対象lane/evidenceを短時間強調し、チャット側にも「Transport laneの18:42:09へ移動しました」のような結果を残す。ユーザーの手動操作をロックせず、手動操作が入ったらguided sequenceを一時停止する。

### 10.3 最初の suggested prompts

- この通話で最も疑わしい事象は？
- この切断について観測できたことと、分からないことは？
- Primary と Compare の差は？
- 次にどの証拠を取れば原因を絞れる？
- この incident の再現実験を設計して

## 11. セキュリティ・プライバシー

診断エージェント導入前に最低限必要な境界:

- Control Plane 全体に operator authentication を導入する。audio だけの短命 cookie では不十分。
- session/run/artifact/tool 単位の authorization hook を設ける。v1 が単一組織でも deny-by-default interface を置く。
- model egress policy を deployment 単位で定義する。external model 禁止、metadata only、redacted logs allowed 等。
- tool call、model request の evidence ref、回答、feedback を監査する。ただし secret/raw payload は監査ログへ複製しない。
- prompt injection をデータとして扱う。log、code comment、transcript、過去 case 内の命令を system instruction として実行しない。
- URL fetch、shell、任意 SQL、任意 path read、任意 network access をモデルツールにしない。
- retention: diagnostic messages/tool results/cases に個別 TTL を設定する。
- deletion: run 削除時に関連 session、claim、case link、artifact を追跡して削除または匿名化する。
- recording/transcript は explicit opt-in。v1 のモデルには送らない。
- UI commandは表示・選択・再生だけを許可し、状態変更APIから型と権限を分離する。

### 11.1 Operator authentication の推奨

operator authenticationとは、「今VoxBenchを操作している人が誰で、どのrun・録音・診断ツールへアクセスできるか」をControl Planeが確認する仕組みである。現在のremote audio session cookieは録音取得だけを保護する限定的な仕組みで、診断session、run metadata、外部LLM送信、UI commandの認可には不足する。

推奨は、VoxBench自身でユーザー/password管理を作らず、既存のidentity providerへOIDCで接続すること。

- production: Google Workspace、Microsoft Entra ID、Okta、Auth0、Keycloak等のOIDC provider。
- self-hosted: KeycloakまたはAuthentikを推奨。
- local development: loopback限定のdevelopment identity providerまたは明示的なsingle-operator dev mode。

WebはAuthorization Code Flow + PKCEを使う。Control Plane/BFFがHttpOnly、Secure、SameSite cookieのsessionを持ち、browserへprovider tokenを永続保存しない。APIはsessionから`operator_id`、organization、role、policyを解決する。

最初のroleは複雑にしすぎず、以下で十分である。

- `viewer`: metadata/timelineを閲覧。
- `diagnoser`: 診断session、外部LLM送信、許可済み録音再生、UI command。
- `operator`: 将来の実験実行や設定変更の承認。
- `admin`: connector、retention、egress policy、role管理。

OSS coreにはprovider非依存の`IdentityProvider`/`Authorizer`契約とOIDC実装を置き、組織固有groupからroleへのmappingはdeployment configに置く。

### 11.2 外部LLM APIの推奨

外部LLM APIの利用を許可する。ただしmodel providerをコアへ固定せず、`ModelAdapter`越しに接続する。初期production adapterは構造化出力、tool calling、streaming、request IDを扱えるproviderを1つ選び、fake adapterを全テストの標準にする。

モデルへ送るデータはdeploymentごとのegress classで管理する。

- `metadata`: safe run metadata、incident、metric、alias。
- `excerpt`: redaction済みlog/source excerpt。
- `content`: transcript/audio由来内容。既定禁止。
- `secret`: 常に禁止。

既定policyは`metadata`のみ許可する。`excerpt`はoperatorの明示操作または組織policyで許可する。録音をモデルに直接送らず、ローカルanalyzerが出した特徴量やevidenceを送る。

## 12. 実装フェーズ

### Phase 0: 安全な内部境界の整理

目的: 挙動を変えず、診断ロジックを再利用可能にする。

- timeline model/projection と incident rules を `run_api.py` から分離。
- rule ID に version を導入。
- evidence ref resolver と coverage summary を実装。
- `App.tsx` から Call inspector と API client/types を component/module 分割。
- 現在の timeline/API snapshot tests を維持。

完了条件:

- 既存 API JSON とUIの挙動が変わらない。
- incident の全 evidence ref が resolver で解決できる。
- missing coverage が lane ごとに機械判定できる。

### Phase 1: 決定論的 Diagnosis API

目的: LLM なしでも価値のある診断 bundle を作る。

- `get_run_context`, `list_incidents`, `get_evidence` service。
- disconnect/media continuity/cross-run analyzer。
- `/runs/{id}/diagnostic-bundle` の内部または管理API。
- privacy-safe exportable diagnostic summary。

完了条件:

- 今回のような「GW発BYEだけでは発信者切断とtimeoutを区別不能」をfixtureで固定。
- 方向別最終media、gap、capture health、unknown boundary を正しく返す。
- LLMなしのJSON/Markdownレポートが生成できる。

### Phase 2: 読み取り専用エージェント MVP

目的: 選択runについて自然言語で証拠付き回答を得る。

- session/message/tool call/claim migration と repository。
- provider-neutral model adapter。
- bounded orchestrator と6個以内のread-only tool。
- structured answer と citation validator。
- SSE streaming と cancel/reconnect。
- Ask VoxBench drawer、context chip、evidence navigation。
- 型付きUI command dispatcherとclient acknowledgement。
- `画面で見る`とstep-by-step guided investigation。

完了条件:

- 引用なし observed claim がUIへ出ない。
- tool budget、timeout、cancel が機能する。
- モデル停止時も既存の deterministic diagnosis を返せる。
- fake model を使う integration test が再現可能。
- エージェントが選んだevidenceへcursor、lane、incident、録音位置が正しく移動する。
- ユーザーが停止または手動操作したときguided sequenceが安全に止まる。

### Phase 3: 過去run比較と検証済みcase

- structured known-case search。
- operator feedback と expert validation。
- matched-run suggestion と config diff。
- case applicability/versioning と stale warning。

### Phase 4: 外部ログ・source snapshot・PCAP

順序はログ、source snapshot、PCAP parser を推奨する。各adapterごとに allowlist、redaction、size/time limit、audit を先に実装する。

### Phase 5: 承認付きアクション

診断品質と認証・監査が十分に検証された後のみ着手する。

- 実験計画を immutable spec として作成。
- config patch の提案と diff preview。
- sandbox/staging での実行承認。
- source patch/PR は別サービスと別権限。

本番設定への直接変更、任意shell、無承認PR作成は対象外とする。

## 13. テスト戦略

### 13.1 golden diagnostic fixtures

最低限、次の fixture を作る。

- assistant送出停止後にGW発BYE。ただしcaller hangupと識別不能。
- RTP無送信でもRTCP/CN継続。media timeout 仮説を確定しない。
- caller側RTPは継続、assistant側のみ停止。
- capture drop があり、sequence gap のconfidenceを下げる。
- provider response中にfinal stage silenceとplayback欠落が重なる。
- stage録音は正常だがremote playoutは未観測。
- config変更前後のmatched runs。
- lane欠落を正常扱いしない。

### 13.2 評価指標

- citation precision: 引用がclaimを実際に支持する割合。
- citation completeness: observed/derived claimの引用率。
- abstention accuracy: 証拠不足時に断定を避けた割合。
- hypothesis recall: known fixture の候補原因を落とさない割合。
- false certainty rate: 誤って certain/high を付けた割合。
- tool efficiency: 解決あたりtool call/token/time。
- user outcome: helpful、調査時間短縮、expert correction率、再発防止率。

モデル更新時は golden questions をCIまたは定期評価で流す。文面完全一致ではなく、claim/evidence/hypothesis/action の構造を評価する。

## 14. 最初の縦切り実装

最初のPR群は以下の順に分ける。

1. timeline/incident model と projection の抽出。機能変更なし。
2. evidence resolver と coverage summary。
3. disconnect analyzer と fixture。
4. deterministic diagnostic bundle endpoint と export。
5. diagnostic session schema/repository/API。
6. fake model + bounded orchestrator + claim validator。
7. SSE と Ask VoxBench drawer。
8. 型付きUI command、evidence navigation、guided investigation。
9. OIDC operator authとrole/authorization hook。
10. production model adapter、egress policy、運用メトリクス。

PR 1〜4までで「チャットなしでも正しい診断データ」を完成させる。この順序により、モデルの回答品質問題と観測・相関ロジックの問題を分離してデバッグできる。

## 15. 未決事項

実装着手前または環境接続時に確認が必要なもの:

- 利用する外部model providerとデータ処理地域。外部API利用自体は許可済み。
- 接続先OIDC provider。方式はOIDC/BFF sessionを採用する。
- ログ基盤の実体（Loki/OpenSearch/CloudWatch/file等）。
- deployment と source commit/digest をどう関連付けるか。
- 音声/transcriptを将来モデルへ送るか。送る場合の同意、redaction、地域、保持期間。
- 診断会話を保存する期間と、検証済みcaseへの昇格フロー。
- OSS core に含める範囲と、環境固有connectorをpluginに置く範囲。

これらが未決でも Phase 0〜2のfake adapterとUI commandまでは進められる。production identity/model/log connectorの接続時に、実環境の値だけを決めればよい。
