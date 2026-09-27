# VoxBench project memory

このファイルは、今後の実装・設計セッションで維持すべきプロダクト判断を短く記録する。

## 現在の状態を読む順序（2026-09-27）

- 実装状況の入口は [implementation status](docs/implementation-status.md)。コミット済み、検証済みlocal candidate、計画、deployment検証残件を分ける。
- 設計は `DESIGN.md`、[診断エージェント設計](docs/diagnostic-agent-design.md)、[cascade設計案](docs/cascade-design.md)を参照する。設計の存在だけで実装済みとは判定しない。
- `PROGRESS.md`と日付付きSerena実装memoryは当時のsnapshot。古い「未実装」や進捗率を現在の状態として再利用しない。
- 公開用worktreeのコミット基準は`ae575b6`。UI commandとlocal diagnostic SSEは未コミットのlocal candidateで、必要な新規module/test/Web fileも未追跡である。詳細と独立した安定化手順はstatus文書に記録した。
- 2026-09-18にlocal candidateを含むPython 3.14.3のsuiteで330 passed / 4 skipped、Ruff、Web build成功を確認。Postgres実接続、browser、production認証、公開CIの検証とは区別する。
- 同日C1実装後の最終検証：現在のPython suiteは408 passed / 4 skipped、Ruff成功。コミット済み基準へC1だけを加えたcheckoutは401 passed / 4 skipped、Ruff成功。78件のC1テストに加え既存テストも成功。後者はUI/SSE candidate不要で、dependencyは既存環境を共有した。
- ユーザーは調査・計画・設計に続いて実装を指示した。candidateを暗黙の公開baselineにせず、cascade契約はコミット済みv1との互換性を基準に実装する。

## STT → LLM → TTS cascade（2026-09-18）

- ユーザー要件：STT、LLM、TTSを個別モデル/providerで組んだcascadeに対応する。既存speech-to-speech realtimeも維持する。
- 合意した方向：既存cascadeの観測を先に、テスト通話の実行を後に進め、実装をissueへ分割する。最初のprovider組合せは未選択。
- issue化済み：[親issue #15](https://github.com/engkimo/voxbench/issues/15)と9件の作業issue（#16–#24）。担当範囲・依存関係・着手順は[issue計画](docs/cascade-issue-plan.md)を参照する。issue公開は機能実装の完了を意味しない。
- 追加ユーザー要件：Pipecat限定にしない。Pipecat以外のframework/middlewareと、middlewareを使わずSDK/HTTP/WebSocketを直接接続する独自実装にも対応する。
- Realtime/Cascadeという音声方式と、frameworkあり/なしという実行方式は独立した軸。coreの観測・診断契約に特定frameworkのframe/context/queueを必須化しない。接続言語もHTTP契約で限定しない。
- 共通service観測契約とconformance fixtureを中心に置く。framework adapterは任意dependency、直接接続の例はframework不要で動かす。専用adapterのないアプリにもapplication-owned hook/HTTP経路を提供する。
- テスト通話はframework非依存の`RuntimeAdapter`/application launcher契約越しに起動する設計。通話runtimeの管理主体、model service adapter、観測transportを分離する。
- 現在もobserver/HTTP ingestとbuilt-in直接接続Realtime経路はPipecat非依存。Pipecat helperは薄い構築境界で、他framework専用adapter、cascade観測・実行、横断conformanceは未実装。
- #17/C1の設定契約をlocal candidateとして実装した。`voxbench/v2`のrealtime/cascade union、service role/modality、input/output PCM chain、一意なcomponent ID、判断主体、manifest版/digest固定とoverlay検証に対応する。v1のresolved object/hashは維持する。現在の契約と使い方は[cascade config v2](docs/cascade-config-v2.md)。
- turn/request/response/TTS segment/epochを明示相関する。並行処理のdurationを足してend-to-end latencyにしない。clockや相関の不足はindeterminateとする。
- 文字列はアプリruntime内で必要だが、VoxBenchのservice観測はmetadata-only。transcript/prompt/LLM文/本文hashを標準保存・診断model送信しない。
- 音声のmedia timeとrun timeをsegment単位で対応付ける。STTとTTS間にaudio duration/level preservationを適用しない。
- C1設定契約だけでは実通話成功を意味しない。C2が観測専用runとservice event ingestを追加した後も、executionとv2音声録音は明示拒否する。受入条件とslice順序はcascade設計文書に置く。
- #18/C2のmetadata-only service観測をlocal candidateとして実装した。v1/v2の観測専用runで、framework非依存の同一DTO/HTTP batchを使用する。collector/event、session/turn/request/parent/response/segment/epochを保持し、本文・URL・raw provider ID・secretのfieldを閉じる。契約は[cascade observation API](docs/cascade-observation-api.md)。
- 同一event IDの同一payload再送だけを冪等とし、変更は409。未知parentをcoverage不足として残し、out-of-order到着後は再構築する。cancel request/ackとrun completeを別証拠とする。observer queueとrun event数をboundしdrop countを公開する。
- #19/C3Aの決定論的因果解析をlocal candidateとして実装した。明示的なparent request chainだけを辿り、STT finalization、turn coordination、LLM dispatch/first output/first answer、aggregation、TTS dispatch/first PCM、local playback、end-to-end local writeを分離する。各値はevidence refs、scope、definition version、clock/uncertaintyを持つ。欠測は`unobserved`、未校正clock間と負の順序は`indeterminate`であり、近接timestampによる推測やservice durationの単純加算はしない。契約は[cascade analysis](docs/cascade-analysis.md)。
- `llm.first_output`はanswer/reasoning/tool_call/otherの種別だけを保持し、本文を保持しない。tool-call continuationとretryは別operation。generation epoch/responseが異なる古いplaybackをend-to-endに再利用しない。
- latency incidentはv2 configの明示的`service_latency_slos`があるobserved measurementだけに生成する。通常metadataからlatency SLO、STT精度、意味品質、発音品質、remote audible timeを推論しない。
- C3A後もaudio segment mapping、inspector、provider/framework test project・adapter、test-call実行は未実装。v2 `/runs/observed`のみ対応し、`/runs`・`/runs/async`とv2 audio chunkは明示拒否する。
- 2026-09-27のC2最終検証：現状は433 passed / 4 skipped、Ruff成功。`ef7deec`へC1+C2だけを加えUI/SSE candidateを除外したcheckoutは426 passed / 4 skipped、Ruff成功。25件のC2 focused test、standalone JSON Schema、SQLite repository再構築を確認。既存dependencyを共有し、実provider/framework/Postgres接続は未検証。
- 2026-09-27のC3A最終検証：`ae575b6`基準の公開用worktreeは444 passed / 5 skipped、Ruff成功。`ef7deec`へC1–C3だけを加えUI/SSE candidateを除外したcheckoutは436 passed / 5 skipped、Ruff成功。10件のC3 focused testとSQLite restart再構築を確認。実Postgres restart testは追加したが環境変数未設定でskip、実provider/Pipecat/browserは未検証。
- ユーザー指定の次ゴール：決定論的fake STT/LLM/TTS Cascadeを、application-owned direct実装（middlewareなし）とPipecat実装の2 test projectとして作る。同じscenario・観測契約・C3解析を通し、VoxBenchで検証、調整、比較評価できるようにする。Pipecatはoptional dependencyのままにし、実provider選定とは分ける。既存issueでは#22と#23のPipecat部分に対応する。

## 診断エージェント（2026-08-07）

### 目的

VoxBenchに、選択中の通話を起点としてコード、ログ、設定、SIP/RTP、録音、metric、provider eventなどを調査する診断エージェントを追加する。

診断レポートを生成するだけでなく、エージェントがVoxBench UIを共同操作し、「この区間を見てください」と根拠を実演できることを製品要件とする。

### 合意済み

- v1は読み取り専用の診断から始める。
- 数値計算、時刻相関、RTP gap、BYEまでの時間、capture health、rule判定は決定論的コードが担当する。
- LLMは調査計画、型付きtoolの選択、仮説比較、説明を担当する。
- claimは`observed`、`derived`、`inferred`、`unknown`、`recommended`に分類する。
- 引用のない`observed` claimはユーザーへ表示しない。
- 証拠の欠落を正常と解釈しない。
- 外部LLM APIの利用を許可する。
- モデルproviderは`ModelAdapter`で交換可能にする。
- 外部送信は初期状態でsafe metadataだけを許可する。redaction済みlog/source excerptはpolicyまたは明示操作で許可し、audio/transcript内容は初期状態では送らない。secretは常に送らない。
- UI操作はDOM selector、任意JavaScript、汎用browser automationで行わない。
- VoxBenchが型付きUI commandを定義し、server側でrun/evidence scopeを検証する。
- 初期UI commandはrun/incident選択、evidenceへの移動、time window、panel表示、録音再生・停止、比較、view filter、guided sequenceに限定する。
- 任意URL、フォーム入力、設定保存、任意API呼び出しはUI commandに含めない。
- guided investigationは既定でstepごとにユーザーが進める。ユーザーが手動操作したら一時停止する。
- operator authenticationはOIDCを採用する。独自password管理は作らない。
- WebはAuthorization Code Flow + PKCE、Control Plane/BFFはHttpOnly/Secure/SameSite cookie sessionを使用し、provider tokenをbrowser storageへ保存しない。
- 初期roleは`viewer`、`diagnoser`、`operator`、`admin`を推奨する。
- 既存のremote audio session cookieは録音取得専用で、製品全体のoperator authの代替にはしない。
- productionでの設定変更、実験実行、コード修正は、将来の承認付きactionとして診断ツールから分離する。

### Production診断への推奨実装順序

以下は残件の設計順序。local UI/SSE経路の存在は後述のcandidate状況とstatus文書で管理し、production永続化・orchestrationの完了とは扱わない。

1. Timeline/incident modelとprojectionを`run_api.py`から分離する。
2. Evidence resolverとcoverage summaryを実装する。
3. Disconnect analyzerとgolden fixtureを実装する。
4. LLMなしのdeterministic diagnostic bundle APIを作る。
5. Diagnostic session/message/tool call/claimを永続化する。
6. Fake model、bounded orchestrator、claim/citation validatorを実装する。
7. 永続event storeをSSEと既存local Ask VoxBench candidateへ接続し、long-running job/cancel/reconnectを実装する。
8. 型付きUI command candidateを安定化し、guided investigationを実装する。
9. OIDC operator authとauthorization hookを実装する。
10. Production model adapterとegress policyを接続する。

### 型付きUI command・local agent candidate（2026-08-15実装、2026-09-18監査）

この節は未コミットcandidateの機能記録。コミット済み製品の機能一覧ではない。SSEは作成済みeventのfinite replayで、質問による調査計画、継続配信、自動再接続、session容量/TTLは未実装。

- `POST /runs/{run_id}/ui-commands/resolve`を追加し、未信頼proposalをPydanticで型検証した後、run内のincident/evidence/artifact/recording/filter/time windowへ解決する。
- Webに検証済みcommand dispatcherを追加した。run/incident選択、evidence focus、time window、panel表示、録音選択・再生・停止、比較、category/stage/direction filterをUI stateへ直接反映する。
- command IDはclientでidempotentに扱う。適用、拒否、browser autoplay blockを結果として表示する。
- 右railの`Agent UI command bridge`は、現在のrunからsafeな例を生成して、人間とfake agentが同じ契約を共同確認するための手動adapterである。
- 任意URL、DOM selector、JavaScript、フォーム入力、設定保存、任意API呼び出しは引き続き許可しない。
- process内memoryを使うlocal deterministic diagnostic sessionを追加した。最も強いincident、なければ最初のtyped eventを選び、引用と検証済みUI commandをordinal付きSSE eventで配送する。
- Webの`Ask VoxBench`がSSE commandを自動実行し、結果をControl Planeへidempotentにacknowledgeする。これにより人間がJSONをpasteせず、fake agentがUIを直接動かすend-to-end経路を確認できる。
- SSEは`Last-Event-ID`/`after`で再生可能。ack済みcommandへ異なる結果を送ると`409`で拒否する。
- 未実装なのは永続session/event/result store、production model adapterとbounded orchestrator、long-running job/cancel、guided sequence、OIDC認可である。local deterministic adapterをproduction診断agentと表現しない。

### 環境接続時に決めること

- 外部model providerとデータ処理地域。
- 実際に接続するOIDC provider。既存のGoogle Workspace、Microsoft Entra ID、Okta等があればそれを優先し、self-hostedではKeycloakまたはAuthentikを候補とする。
- Asterisk/application logの基盤と`LogSourceAdapter`実装。
- runとdeployment commit/artifact digestの関連付け方法。
- 診断message、tool result、caseの保持期間。
- 将来audio/transcriptを扱う場合の同意、redaction、保存地域、保持期間。

### 重要な診断原則

今回の例のように、`GW -> Asterisk BYE`だけでは、発信者の手動切断と中間PBX/GWのtimeoutを識別できない。エージェントは、観測できたBYE方向と時刻を示しつつ、識別不能な境界を`unknown`として残す必要がある。
