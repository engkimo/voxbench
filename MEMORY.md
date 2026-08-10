# VoxBench project memory

このファイルは、今後の実装・設計セッションで維持すべきプロダクト判断を短く記録する。詳細設計は [診断エージェント設計・実装計画](docs/diagnostic-agent-design.md) を参照する。

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

### 推奨実装順序

1. Timeline/incident modelとprojectionを`run_api.py`から分離する。
2. Evidence resolverとcoverage summaryを実装する。
3. Disconnect analyzerとgolden fixtureを実装する。
4. LLMなしのdeterministic diagnostic bundle APIを作る。
5. Diagnostic session/message/tool call/claimを永続化する。
6. Fake model、bounded orchestrator、claim/citation validatorを実装する。
7. SSEとAsk VoxBench drawerを実装する。
8. 型付きUI commandとguided investigationを実装する。
9. OIDC operator authとauthorization hookを実装する。
10. Production model adapterとegress policyを接続する。

### 環境接続時に決めること

- 外部model providerとデータ処理地域。
- 実際に接続するOIDC provider。既存のGoogle Workspace、Microsoft Entra ID、Okta等があればそれを優先し、self-hostedではKeycloakまたはAuthentikを候補とする。
- Asterisk/application logの基盤と`LogSourceAdapter`実装。
- runとdeployment commit/artifact digestの関連付け方法。
- 診断message、tool result、caseの保持期間。
- 将来audio/transcriptを扱う場合の同意、redaction、保存地域、保持期間。

### 重要な診断原則

今回の例のように、`GW -> Asterisk BYE`だけでは、発信者の手動切断と中間PBX/GWのtimeoutを識別できない。エージェントは、観測できたBYE方向と時刻を示しつつ、識別不能な境界を`unknown`として残す必要がある。
