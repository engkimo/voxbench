# VoxBench（仮称）全体設計書 v0.1

> AI音声エージェントを電話網に載せる時に必ず壊れる4つ（レベル／尺／等時性／リソース衛生）を、
> パイプラインのステージ単位で宣言・検証・可視化し、劣化したステージ・その時のconfig・
> パケット文脈を1画面で特定する、セルフホストの実験＆可観測プレーン。
>
> 名前は仮。パッケージ名 `voxbench`。Apache-2.0。

このドキュメントは Claude Code / Codex への実装ハンドオフ用。**§1（存在理由）と §2（非目標）を最優先で守ること。** 特定スタック（Gemini / Asterisk / 8kHz μ-law）の値は実装のどこにもハードコードしない。それらは `/examples` 配下のサンプルプラグイン設定として“データ”でのみ存在する。

---

## 1. 存在理由：劣化モデル（この製品の背骨）

AI音声エージェントは**ブラウザ（WebRTC）では動くのに、電話（SIP/RTP, 8kHz μ-law, 固定20ms cadence）に載せると壊れる**。WebRTCはレート整合・等時性・バッファをSDKが面倒を見るが、電話に繋いだ瞬間、レート変換・μ-law化・ペーシング・リソース制約がむき出しになる。

壊れ方は4つの不変条件の破れに分類できる。解法も4種に分類できる。

| 劣化モード | 不変条件 | 観測指標 | 典型解法 |
|---|---|---|---|
| レベル不足/過大 | `level_preserving` | 入出力RMS、ゲイン張り付き率 | params調整 |
| 尺短縮（子音脱落） | `duration_preserving` | 尺保存率（秒） | 依存ライブラリ内部定数の上書き |
| 等時性破れ（プツプツ） | `isochronous` | frames_out/in比、フレーム間隔ジッタ | 処理をペーシング可能な位置へ移動 |
| リソース衛生 | （宿主の健全性） | CPU、active task数、loop lag、通話横断トレンド | ライフサイクル/停止フック |

製品の中核機能は、この表を**自動化**すること：各ステージにタップを刺し、宣言された不変条件を検証し、破れたステージとその時のconfigを特定する。「Geminiは正常、経路のどこかで劣化」を人手で切り分ける作業を製品が吸収する。

---

## 2. スコープと非目標（ガードレール）

### 2.1 v1スコープ
- エンジン：Asterisk（chan_websocket）1種。
- プロバイダ：Gemini Live 1種。
- ただし**コードのどの経路もGemini/Asteriskを前提にしてはならない**。両者は必ずプラグイン＋manifest越しに扱う。コアは両者を知らない。

### 2.2 非目標（やらないこと）
- **PipeCatを作り直さない。** オーケストレーションエンジンとして薄くラップするだけ。プロバイダ抽象（Gemini/OpenAI/Bedrock）はPipeCatに肩代わりさせる。
- **Langfuse / ClickHouse / Redis をコアに同梱しない。** Postgresファースト。制御プレーン自身がOTLPシンクになる。ClickHouse/Timescaleは `--profile scale` のopt-inのみ。
- **特定値をハードコードしない。** `8000`, `target_rms=3000`, `CLEAR_STREAM_AFTER_SECS`, `silence_duration_ms=600` 等は全て `/examples` のサンプルconfig/manifest内のデータ。コアやプラグイン実装本体に定数として書かない。
- **プロバイダ固有ロジックをコアに入れない。** turn_taking所有権・対応codec・許可override等は全てmanifestで宣言。
- **ブラウザストレージ（localStorage等）をUIで使わない。**
- v1で**マルチテナント認証基盤・課金・RBACは作らない**（単一組織セルフホスト前提。後付け可能な構造にはしておく）。

### 2.3 設計原則
- コアスキーマは「封筒」と「拡張点の契約」だけを持つ。具体パラメータは全てプラグインのJSON Schemaに逃がす。
- 1つの相関キー `run_id`（SIP Call-ID / PipeCat conversation_id と双方向マップ）を全テレメトリに刻む。製品の価値は**この結合（JOIN）**にある。
- 不変条件は**ステージごとにmanifestで宣言**する。グローバルに仮定しない（例：AGCステージは `level_preserving` を意図的に破る）。

---

## 3. アーキテクチャ全体

```
┌─────────────────────────── Web UI (React/TS) ───────────────────────────┐
│  設定エディタ / 実験デザイナ・比較 / コール検査(統合タイムライン) / ライブ監視  │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ REST + WS
┌───────────────────────────────▼──────────── Control Plane (FastAPI) ─────┐
│  ConfigRegistry  PluginRegistry  ExperimentOrchestrator  SessionController │
│  Collectors(OTLP/HEP/host)  VerificationEngine  ScoringWorkers  SynthCaller│
└───────┬───────────────────────────────────────────────────┬──────────────┘
        │ resolved config (run_id付与)                        │ telemetry (run_id刻印)
┌───────▼──────────── Engine Harness (PipeCat wrapper) ───────┴──────────────┐
│  config→Pipeline構築 / 各境界にStageTap / OTel emit / host metrics sampler   │
│  plugins: engine(asterisk) · provider(gemini) · processor(resampler/agc/…)  │
└───────┬───────────────────────────────────────────────────────────────────┘
        │ SIP/RTP                                   ▲ 合成発信(参照音声+雑音)
   Asterisk ── 電話網                          SyntheticCaller
                                ┌──────────── Storage ──────────────┐
                                │ Postgres(必須) + MinIO(録音/PCAP)  │
                                │ [scale] ClickHouse/Timescale 任意  │
                                └────────────────────────────────────┘
```

構成要素：

1. **Control Plane（FastAPI）** — アプリ本体。後述の各サービス。
2. **Engine Harness** — PipeCatの薄いラッパ。resolved configを読み、宣言されたプラグインでPipelineを組み、各ステージ境界にStageTap（録音＋frame timing）を差し込み、OTelスパンに `run_id` を刻んで吐き、host metricsをサンプリングする。
3. **Storage** — Postgres（必須）＋ MinIO（録音/PCAP）。scaleプロファイルでClickHouse/Timescale。
4. **Web UI** — React/TS。コール検査の統合タイムラインが主役。

### 3.1 Web first-use contract

- 初回ユーザーはprovider keyや電話環境を用意せず、画面先頭の1操作で3秒の可聴demoを完了できる。
- 録音とstage差分を診断情報より先に見せ、run作成後は対象runを自動選択する。
- unknown/readiness未確認をfailureやpassとして扱わず、`not checked` / `not measured`として区別する。
- WebSocketは即時性の最適化であり必須条件ではない。切断時はstale snapshotを残さずREST pollingへ退避する。
- 合成tone demoはgain/pipeline動作の決定的な確認用であり、speech品質評価や実通話検証の代替とはしない。

---

## 4. ドメインモデル / DBスキーマ（Postgres）

`jsonb` を多用。全テーブルのタイムスタンプ・UUID PK省略表記。

```sql
-- プラグインとcapability manifest（§6）
plugins (
  id uuid pk, kind text,        -- 'engine' | 'provider' | 'processor'
  name text, version text,
  manifest jsonb,               -- capability manifest 全体
  unique(kind, name, version)
)

-- 設定（§5）。publishで不変、hashが再現キー
configs (
  id uuid pk, name text, version text,   -- 人間用 semver
  parent_id uuid null,                    -- overlayのベース
  spec jsonb,                             -- 著者が書いた差分(overlay可)
  resolved jsonb,                         -- 解決済み完全config
  hash text,                              -- sha256(resolved) = 再現キー
  status text,                            -- 'draft' | 'published'
  labels jsonb
)

-- テストシナリオ（再現可能な入力）
scenarios (
  id uuid pk, name text,
  kind text,                    -- 'synthetic' | 'replay' | 'online'
  reference_audio_uri text null,-- 合成/リプレイ時のクリーン参照
  noise_profile jsonb null,     -- {type:'babble'|'street'|'echo', snr_db:..}
  script jsonb null             -- 発話スクリプト(合成発信者用)
)

-- 実験
experiments (
  id uuid pk, name text, scenario_id uuid,
  arms jsonb,                   -- [{config_id, label}]
  n_calls int, assignment text, -- 'random' | 'sequential'
  metric_set jsonb,             -- 集計対象メトリクス名
  status text
)

-- ★相関アンカー。1コール=1run
runs (
  id uuid pk,                   -- = run_id (相関キー)
  experiment_id uuid null, arm_label text null,
  config_id uuid, config_hash text,
  scenario_id uuid null,
  call_id text,                 -- SIP Call-ID（双方向マップ）
  conversation_id text,         -- PipeCat側
  provider text, engine text,
  status text,                  -- 'running' | 'completed' | 'failed'
  failure_alias text null,      -- safe fixed/operator alias; raw exceptionは保存しない
  resolved_config jsonb,        -- restart後のtimeline/verification再構築に必要
  environment_metadata jsonb,    -- aliases/references only; no secret values/URLs/Slack IDs
  readiness_checklist jsonb,     -- [{item_id,label,status,note}]
  started_at, ended_at
)

-- ステージごとのタップ録音
recordings (
  id uuid pk, run_id uuid, ordinal int, stage text,
  uri text,                     -- MinIO上のWAV
  format jsonb,                 -- {rate, encoding, channels}
  duration_ms numeric
)

-- ★不変条件の検証結果（§7）
verifications (
  id uuid pk, run_id uuid, ordinal int, stage text,
  invariant text,               -- 'duration_preserving' 等
  passed bool,
  observed jsonb, expected jsonb, detail text
)

-- 信号＋ホストメトリクス（時系列。MVPはPG、scaleでCH/TS）
metrics (
  id bigserial pk, run_id uuid, ordinal int, stage text null,
  name text,                    -- 'rms_out','duration_ratio','frame_cadence_jitter','cpu','active_tasks','loop_lag' 等
  value numeric, ts timestamptz
)

-- OTLPトレース（自前シンク。最小）
spans (
  id uuid pk, run_id uuid, ordinal int, trace_id text, span_id text, parent_id text null,
  name text, start_ns bigint, end_ns bigint, attrs jsonb
)

-- SIP/RTP（任意・v1後半）。HEP/collector取込。raw packet/bodyは保存しない。
sip_events (
  id pk, run_id uuid, ordinal int, call_id text null, method text, direction text,
  status_code int null, summary_alias text null, ts
)
rtp_stats  ( id pk, run_id uuid, ordinal int, ts, jitter_ms numeric, loss_pct numeric, mos numeric )

-- 永続run実行queue。claimはFOR UPDATE SKIP LOCKED、lease tokenをfencingに使用。
run_jobs (
  id uuid pk, run_id uuid unique,
  state text,                   -- 'queued' | 'leased' | 'completed' | 'failed'
  available_at timestamptz,
  lease_owner text null, lease_token uuid null, lease_expires_at timestamptz null,
  attempts int, failure_alias text null
)
```

Postgres workerの結果確定は、対象`run_jobs` rowをlockし、run/job/worker/lease token/期限を
再検証した同じtransaction内で、run本体・正規化child rowsのreplaceとjobのterminal化を行う。
stale/expired workerは更新件数0相当で拒否し、結果だけ・jobだけがcommitされる中間状態を作らない。
通常の同期run/observed mutationは従来のrepository save契約を維持する。
Postgres `/runs/async`のinitial run保存とqueued job作成も同一transactionで行い、runだけが残る
crash windowを作らない。memory modeはlocal互換のprocess内daemon executionを維持する。

---

## 5. Config スキーマ（確定稿）

コア＝封筒＋拡張点。具体値はプラグインparamsへ。

```yaml
apiVersion: voxbench/v1
kind: VoiceConfig
meta:
  name: "baseline"
  version: "1.0.0"          # 人間用 semver
  parent: "<config_id>"     # 任意。overlayのベース
  # hash, resolved は登録時に制御プレーンが計算
  labels: { env: dev }

spec:
  engine:                   # メディア/テレフォニ実行体
    kind: asterisk          # plugins(kind=engine) のname
    params: { ... }         # engineプラグインのparam_schemaで検証

  transport:                # SDPレベルの普遍部分のみ
    codec: opus
    ptime_ms: 20
    jitter_buffer: { mode: adaptive, max_ms: 200 }

  media:                    # 順序付きパイプライン。固定フィールドにしない
    pipeline:
      - type: resampler
        plugin: soxr-stream
        params: { ... }
        # io / invariants / lossy_expected / requires_host_capability / overrides
        # は基本 manifest から継承。ここで個別上書き可
      - type: agc
        plugin: rms-target
        params: { target_rms: 3000, max_gain: 8.0, noise_floor: 200 }
      - type: limiter
        plugin: peak-limiter
        params: { ceiling: 0.7 }
      - type: serializer
        plugin: asterisk-ws
        params: { ... }

  turn_taking:              # プロバイダ非依存の普遍軸
    owner: server_vad       # server_vad | client_vad | semantic | external
    detector:               # owner != server_vad の時に使用
      plugin: silero-vad
      params: { silence_ms: 600, threshold: 0.5 }
    barge_in: true

  ai:
    provider: gemini        # plugins(kind=provider) のname
    model: "..."
    params: { ... }         # providerプラグインのparam_schemaで検証
    system_prompt_ref: "<prompt_id@version>"   # 外部参照（差分をクリーンに）
    tools: [ ... ]

  observability:
    stage_taps: true
    record_audio: true
    trace_sample: 1.0
    signal_metrics: [rms_io, duration_ratio, frame_cadence_jitter]
    host_metrics:   [cpu, active_tasks, loop_lag]
    cross_session:  [task_count_trend, mem_trend]
```

**overlay解決**：著者は `parent` ＋差分で書く。登録時に制御プレーンが parent を再帰解決→マージ→`resolved` を生成→`hash=sha256(resolved)` を計算。**runには必ず resolved を pin して hash を刻む**（差分のままだと再現性が壊れる）。

---

## 6. Capability Manifest 契約（特化を防ぐ要）

各プラグインが「自分が何を要求し、何を保証し、何を上書きしてよいか」を宣言する。制御プレーンはconfigをこのmanifestに突き合わせて**実行前に静的検証**する。

```yaml
kind: processor              # engine | provider | processor
name: soxr-stream
version: "1.0.0"
param_schema: { <JSON Schema> }     # params の検証

# このステージの入出力フォーマット契約
io:
  mode: rate_changing               # passthrough | rate_changing | format_changing
  accepts: { encoding: pcm16, channels: 1 }
  # rate は params/config で確定

# このステージが保証する不変条件
invariants_enforced: [duration_preserving]
# このステージに適用可能（＝検証してよい）不変条件
invariants_applicable: [duration_preserving, isochronous]

# 許容される情報損失（偽陽性防止）。例：μ-law 8kHz化の高域損失は正常
lossy_expected: []

# この処理が宿主に要求する能力。満たさない位置に置いたらconfig検証で弾く
requires_host_capability: [cadence_pacing, output_buffering]

# 上書きしてよいライブラリ内部定数の許可リスト（モンキーパッチの安全管理）
allowed_overrides:
  - target: "pipecat.audio.resamplers.soxr_stream_resampler.CLEAR_STREAM_AFTER_SECS"
    type: float

# providerプラグイン専用フィールド（processorでは無視）
provider_caps:
  turn_taking_owners: [server_vad, client_vad]
  supported_codecs: [pcm16]
  input_rate: 16000
  output_rate: 24000
```

**クロスフィールド検証の例**（制御プレーンが実装すべきルール）：
- `turn_taking.owner` が provider の `provider_caps.turn_taking_owners` に含まれるか。
- `server_vad` と `client_vad` の二重構成（detector指定 ＋ owner=server_vad）を**hard-fail**で弾く。
- パイプライン上で `rate_changing` プラグインが `requires_host_capability: [cadence_pacing]` を要求するのに、その位置に cadence能力が無い → **hard-fail**（事例4の根因をconfig段で防ぐ）。
- 隣接ステージの io 契約が不整合（24k出力→16k期待入力）→ **hard-fail**。
- `overrides` が `allowed_overrides` 外の定数を触ろうとしたら **hard-fail**。
- 検証は hard-fail / warn を manifest 側 or レジストリ設定で選べるようにする（既定はhard-fail）。

---

## 7. 検証エンジン（novel core）

1コール完了後、ScoringWorkerがStageTapの成果物を使って各ステージ/隣接ペアを検証する。

### 7.1 不変条件チェック（秒単位・サンプル数ではない）
- `duration_preserving`：`|out_dur_sec − in_dur_sec| < ε`（レート変化があっても秒は保存される）。
- `level_preserving`：`out_rms/in_rms` が許容帯内（AGC等 `invariants_enforced` に持たないステージには適用しない）。
- `isochronous`：`frames_out/frames_in ≈ 1` かつ フレーム間隔ジッタ `< ε`。事例4の `7/50` 型を検出。
- `lossy_expected` 例外：`bandwidth_limit` 宣言があれば高域成分を比較対象から除外（μ-law 8kHz偽陽性を防ぐ）。

### 7.2 客観メトリクス
- 参照あり（synthetic/replay）：ViSQOL（本命, Apache-2.0）、必要時 PESQ。**ステージの期待フォーマットで参照を取る**（8kHzステージは8kHz帯域制限参照と比較）。
- 参照なし（online）：RMS、duration_ratio、frame_cadence_jitter、（RTPがあれば）G.107 E-model MOS。

### 7.3 ホスト/横断
- host_metrics：`cpu`, `active_tasks`, `loop_lag` をrun中サンプリング。
- cross_session：runをまたいで `active_tasks` / mem が**単調増加**していないか検出（事例4の幽霊タスク累積）。

結果は `verifications` / `metrics` に書き、タイムラインで違反をハイライトする。

---

## 8. Synthetic Caller（再現性＋参照信号）

合成発信者を内蔵することで「実発信は制御不能でA/B比較がノイズだらけ」「full-reference metricsはクリーン参照が要る」を同時に解く。

- 既知のクリーンWAV（reference_audio）＋ 制御された雑音プロファイル（babble/street/echo, SNR指定）を合成。
- エンジンへSIP発信（v1はAsteriskにAudioSocket/WS経由でテストコールを注入）。
- これにより `target_rms` 等のチューニングや「途切れ」を**決定論的に再現**でき、ViSQOL/PESQ用の参照が常にある。
- `scenarios.kind = synthetic` がこれ。`replay`（過去の実コール音声を再注入）、`online`（本番shadow）も同じ枠で扱う。

---

## 9. 相関キーとテレメトリ

`run_id` を唯一の結合キーとし、以下すべてに刻む：
- OTelスパン属性（`voxbench.run_id`, `sip.call_id`, `conversation_id`）。
- StageTap録音のオブジェクトキー（`{run_id}/{stage}.wav`）。
- PCAP・SIPイベント・RTP統計・全 metrics/verifications 行。

テレメトリ経路（自前シンク、追加インフラ最小）：
- Engine Harness → **OTLP/HTTP** → 制御プレーン `/v1/traces` → `spans` テーブル。devプロファイルではJaegerにもfan-out可。
- host_metrics → `/v1/host-metrics` → `metrics`。
- （任意）Asterisk → HEP → UDP receiver → `sip_events`/`rtp_stats`。

---

## 10. 統合タイムライン（データ契約）

`GET /runs/{id}/timeline` が、共通 t0（コール開始）で時間整列したレーン群を返す：

```json
{
  "run_id": "...", "t0": "...", "config_hash": "...",
  "environment": {
    "environment_profile": "demo",
    "server_alias": "demo-host-a",
    "integration_target_alias": "integration-target-a",
    "manual_blockers": ["route-confirmation"],
    "tags": ["phase4"],
    "secret_ref_names": ["provider-api-key-ref"]
  },
  "readiness_summary": {
    "passed_count": 3,
    "failed_count": 1,
    "unknown_count": 2,
    "manual_blocker_count": 1,
    "incomplete_count": 4
  },
  "lanes": {
    "sip_ladder":   [ {"ts":0,"method":"INVITE","dir":"in"} ],
    "rtp_quality":  [ {"ts":0,"mos":4.1,"jitter":3,"loss":0} ],
    "stages": [ {
        "stage": "MediaSender",
        "metrics": [ {"ts":0,"name":"duration_ratio","value":0.94} ],
        "violations": [ {"invariant":"isochronous","passed":false,"detail":"frames_out/in=7/50"} ]
    } ],
    "turns":        [ {"ts":0,"kind":"user","event":"start","ttfb_ms":null} ],
    "host":         [ {"ts":0,"name":"cpu","value":78} ],
    "recordings":   [ {"stage":"MediaSender","uri":"..."} ]
  }
}
```

UI挙動：違反ステージを赤マーカー→クリックでその瞬間のSIP/RTP/host文脈へジャンプ。隣接ステージの録音をA/B再生。「botが黙った瞬間にCPUが張り付いていたか」が1画面で分かる。

### 10.1 Typed common-time-axis projection

既存の互換laneに加え、UIとcollectorの共通契約として以下5 primitiveを返す。

- `events`: SIP、VAD、cancel、queue clear、packet gapなどの点。
- `intervals`: speech、provider generation、playback、span、silence/gapなどの区間。
- `series`: RMS/gain、jitter/loss、queue depth、CPU/loop lagなどの時系列。
- `artifacts`: Stage WAV、report、明示的lab modeのrestricted captureなどの参照。
- `incidents`: rule/versionとevidenceを束ねた人間向け診断。証明できない因果は断言しない。

primitiveは`run_id`に加え、source clock domain、run起点の相対ms、alignment uncertainty、collector source、
direction、stage、stream aliasを表現できること。PCAP capture time、RTP timestamp、provider clock、host
monotonic、wall clockを同一精度と仮定しない。clock関係が保証できない片道遅延は`indeterminate`とする。

既存のmetrics/spans/SIP/RTP/recordings/verificationsは後方互換projectionとして維持する。collectorが観測した
因果イベントは`timeline_events`へsafe scalar attributesと相関aliasだけを永続化し、raw provider payload、
provider item ID、URL、secretを持ち込まない。barge-inはprovider VAD通知、interrupt経路、truncate位置、
playback queue clear、完了を同じ相関aliasで結ぶ。queueから破棄した時間は観測値だが、相手に実際に聞こえた
可聴テールではないため、そのようには表示しない。media-gapやtyped turnも同じevent境界を利用する。

RTP集計観測からはdirectionごとにloss 1%以上、jitter 30ms以上、MOS 3.5以下をprovisional warning
contractとしてevent化する。5秒以内の連続観測だけを1 degradation windowへまとめ、連続する2点以上でlossが
閾値を超えた場合も`loss burst suspected`（medium confidence）に留める。RTP sequence numberまたはpacket arrival
cadenceなしにpacket gapや音切れを確定しない。閾値はincidentのExpected contractへ必ず返し、将来はscenario/
manifest ruleへ移す。

Stage間診断は既存verificationを再判定せず、確定した`duration_preserving` failureをmedia-time evidenceへ変換する。
短縮後のoutput durationから直前Stageのinput durationまでを`media_time_missing` intervalとし、隣接する2つの
recording artifactとcontraction eventを同じincidentへ結ぶ。level変化は同一timestampのinput/output RMS、
delta dB、gain appliedを束ね、Stageごとに絶対値最大の変化（1dB以上）をevent化する。RMS/dBをLUFSや知覚
loudnessと呼ばない。LUFS、true peak、clipping率は専用collectorを追加してから別contractとして扱う。

PCM sample quality collectorはStage outputごとにsample peak dBFS、PCM16 extrema sample率、-60dBFS以下の
sample率、chunk durationを測る。extrema sampleはclippingの必要条件になり得るが十分条件ではないため、最初に
出現したStageだけを`Clipping suspected`（medium confidence）とし、waveform plateauまたはoversampled true
peakなしにclippingを確定しない。98%以上digital silenceが200ms以上連続した区間はevidenceとして表示するが、
発話区間との重なりが分からない段階では異常incidentにしない。sample peakをintersample true peakと呼ばない。

provider response lifecycleのscalar metricは、開始/完了eventと`provider_response` intervalへ同じcorrelation
aliasで投影する。その区間と最終pipeline Stageのdigital silenceが200ms以上重なる場合だけ、
`Assistant output dead air suspected`（medium confidence）を生成する。途中Stageの無音は下流で回復し得るため
対象外、provider区間外の無音は正常pauseと区別できないためevidence-onlyのままとする。最終Stage PCMは
assistant出力境界の証拠だが、remote endpointのjitter buffer、decode、speaker playoutは観測していない。
したがって「相手に無音が聞こえた」とは断定せず、incidentのObserved/Expectedにremote playout未観測を明記する。

provider input VADのspeech start/stopは同じsafe correlation aliasで`caller_speech` intervalへ投影する。stop通知が
欠けた場合はrun終端までの区間とし、bridge close時に`completion_observed=false`を残す。これはprovider VADの
判断区間であり、sample-accurateなcaller waveform segmentationではない。

assistant playbackはAudioSocket bridgeがframeをsocketへ書き始めた点をstart、最後に書いたframeの予定時間経過、
barge-in、stream end、call closeをstopとしてburst単位で記録する。前frame完了後の待ちがそのframe durationを
超えた場合はmedia gapとしてburstを分ける。provider item IDは保存せずprocess-local safe aliasだけを使い、
`written_audio_ms`はAsteriskへ書いたPCM時間を表す。remote jitter buffer、decode、speaker playoutを観測した値
ではない。playback evidenceを持つrunではdead-air相関をprovider response・最終Stage silence・playbackの
3区間intersectionへ狭め、bridgeが音声を書いていない生成待ちをdead airと誤認しない。旧runはprovider lifecycle
だけを使うfallbackを維持する。

provider response開始から、そのresponseに対応する最初のplayback burst開始までを
`assistant_output_start_wait` intervalとして返す。response完了後にbridge bufferからwriteが始まる場合もあるため、
次response開始までは同じresponseへ時系列で関連付ける。playbackが観測されなければresponse終端までを未完了wait
として表示する。scenario/manifestのlatency SLOがない段階では、長さだけで遅延failureを作らない。

`media_gap`で分割された隣接playback burst間は`assistant_playback_gap` intervalとする。gapとprovider responseの
重なりが200ms以上の場合のみ`Assistant playback underrun suspected`（medium confidence）へ昇格する。200msは
provisional warning thresholdとしてExpected contractへ返し、将来scenario policyへ移す。provider response activeは
連続音声契約の証明ではなく、AudioSocket frame不在もremote playout断の証明ではないため、
`continuous_audio_contract`と`remote_playout`の未観測境界をincidentへ明記する。

別ビュー `GET /runs/cross-session-trends`：通話横断で単調増加するリソースを表示（リーク検出）。

---

## 11. API surface（主要のみ）

```
# Config / Plugin
POST /configs                      # 登録(draft)
POST /configs/{id}/resolve         # parent解決→検証→resolved+hash
POST /configs/{id}/publish
GET  /configs/{id}
POST /plugins                      # manifest登録
GET  /plugins?kind=processor

# Experiment / Run
POST /experiments
POST /experiments/{id}/run         # シナリオに従いN runを発火・割当
GET  /experiments/{id}/results     # config別集計＋有意差
POST /runs                         # 単発コール実行(config指定, run_id発行)
POST /runs/async                   # running runを即返しbackground実行
GET  /runs                         # recent run summaries
GET  /runs/example-payload         # examplesベースのRunCreateRequest payload
GET  /runs/live-preview            # recent run status + readiness + latest host metrics
GET  /runs/{id}
GET  /runs/{id}/timeline           # §10
GET  /runs/{id}/recordings/{stage}/audio
                                      # local WAV or opt-in bounded MinIO proxy; Bearer/session auth
GET  /runs/cross-session-trends
GET  /storage/readiness            # credential-free storage/proxy capability projection
GET  /repository/readiness         # safe DB state + persistent job-queue capability only
GET  /auth/remote-audio/session     # browser session capability/status; no secret reflection
POST /auth/remote-audio/session     # one-time operator token exchange -> signed HttpOnly cookie
DELETE /auth/remote-audio/session   # clear browser audio session cookie

# Synthetic caller
POST /synthetic-caller/calls       # 参照音声+雑音で発信

# Telemetry ingest
POST /v1/traces                    # OTLP/HTTP 受け口（自前シンク）
POST /v1/host-metrics
POST /v1/sip-events                # structured SIP ladder event; no raw body/SDP
POST /v1/rtp-stats                 # structured RTP quality point
# HEP UDP listener（任意・別ポート）

# Live
WS   /live                         # live-preview projectionをsnapshot push
```

---

## 12. 技術スタック

**Backend（制御プレーン）**
- Python 3.12 / FastAPI / Pydantic v2（config・manifest検証）。
- SQLModel（or SQLAlchemy）＋ Alembic（migration）。
- ワーカ：MVPはSQLAlchemy/Postgresのlease queue（`SKIP LOCKED`＋fencing token）。結果保存も
  active lease検証とjob terminal化を同一transactionに閉じる。Redis不要でPostgresファーストを
  維持し、必要ならprocrastinate/arq/Celeryへ差替可。
- worker実行単位は1 jobだけをclaimし、harness実行中は`Event`で停止可能なheartbeat threadを維持する。
  heartbeat拒否/DB失敗/停止timeoutはlease lostとしてlocal resultを破棄する。途中attemptはqueue retry、
  final attemptだけfailed runとfailed jobをfenced transactionで同時確定する。
- FastAPI lifespanがprocessごとにpolling supervisorをstart/stopする。idle 0.05〜10秒、error 0.1〜60秒、
  shutdown join 0.1〜30秒をboundedにし、既定は0.25/1/5秒。queued/expired leaseをstartup後にclaimする。
- psycopg sessionへ100〜30,000msの`statement_timeout`（既定5,000ms）を設定する。readinessは
  worker enabled/runningとprocess-local processed/error/lease-lost countだけを返し、identity/token/error
  detailは返さない。実Postgres concurrency testはdisposable DBのunique schema内だけで行う。
- OTLP受信：FastAPIエンドポイントで自前パース→`spans`。

**Engine Harness**
- PipeCat（ラップ）＋ OpenTelemetry SDK。
- 音声/採点：numpy, soxr, silero-vad, pesq, visqol（or pip `visqol`）, librosa。
- host metrics：psutil ＋ asyncio loop lag（`loop.time()`差分）＋ `len(asyncio.all_tasks())`。

**Storage**
- Postgres 16 / MinIO。`--profile scale`：ClickHouse or TimescaleDB（`metrics`/`spans`/`rtp_stats`を移送）。

**Frontend**
- React + TypeScript + Vite / Tailwind / shadcn-ui / TanStack Query。
- タイムライン：d3 or visx。音声：wavesurfer.js。

**Packaging**
- Docker Compose。プロファイル：`core`（PG+MinIO+control-plane+harness）, `scale`（+CH/TS）, `dev`（+Jaeger）。
- License：Apache-2.0。プラグインSDKを切り出し、3rdパーティがmanifest付きプラグインを書ける構造に。

---

## 13. リポジトリ構成（monorepo）

```
voxbench/
├─ control-plane/                 # FastAPI
│  ├─ api/  registry/  experiments/  collectors/
│  ├─ verification/  scoring/  synthetic_caller/
│  ├─ models/  migrations/
├─ engine-harness/                # PipeCatラッパ
│  ├─ core/                       # config→Pipeline構築, run_id配線
│  ├─ taps/                       # StageTap(録音+frame timing)
│  ├─ telemetry/                  # OTel emit, host metrics sampler
│  └─ plugins/
│     ├─ engine/asterisk/
│     ├─ provider/gemini/
│     └─ processor/{resampler,agc,echo_cancel,limiter,serializer,vad}/
├─ plugins-sdk/                   # manifestスキーマ, BaseProcessor/BaseProvider/BaseEngine
├─ web/                           # React/TS
├─ schemas/                       # config / manifest の JSON Schema + examples
├─ examples/                      # ★具体値(8kHz等)は全てここにデータとして
│  ├─ configs/  scenarios/  noise-profiles/
└─ deploy/                        # compose, profiles, (later) helm
```

各プラグインは `manifest.yaml` ＋ 実装 ＋ paramのJSON Schema を同梱。コアはプラグインを動的ロードする。

---

## 14. フェーズ計画（薄い縦切り→拡張）

各フェーズに受け入れ基準（acceptance）を付す。コーディングエージェントはこの順で実装する。

**Phase 0 — スキーマとレジストリ（呼び出し無し）**
- config/manifest のJSON Schema、`plugins`/`configs` モデル、overlay解決＋hash、§6のクロスフィールド検証。
- accept：サンプルconfigを登録→resolve→hash算出、矛盾config（二重VAD/契約不整合/不許可override）がhard-failで弾かれる。

**Phase 1 — Engine Harness 単発コール**
- PipeCatラップでGemini+Asteriskのパイプラインをresolved configから構築。各境界にStageTap、OTel emit（run_id刻印）、録音をMinIOへ。
- accept：`POST /runs`で1コール→ run_id発行、ステージ別WAVとspansが保存される。

**Phase 2 — 検証エンジン＋合成発信者**
- 3つの信号不変条件（duration/level/isochronous）＋ lossy_expected例外。ViSQOL/RMS/duration/cadence。Synthetic Caller（参照音声＋雑音）。
- accept：合成コールで各ステージのpass/failが自動判定され、尺短縮/プツプツ/レベル不足を仕込んだサンプルconfigで該当不変条件がfailする。

**Phase 3 — Web コール検査タイムライン**
- §10のタイムライン（stages/turns/host/recordings レーン、違反ハイライト、A/B再生）。設定エディタ（MVPは固定フォーム）。2-config比較。
- accept：1コールの劣化ステージが赤く出て、隣接録音を聴き比べられる。2 config をテーブル比較できる。

**Phase 4 — ホスト/横断＋ライブ監視**
- 前段：run environment metadata、readiness checklist、manual blockers、secret reference names、host_metricsレーン、live-preview、background run、`WS /live`。
- 後段：cross-session trend（リーク検出）、実 live host / SIP / RTP 取込。
- accept（前段）：demo/integration run の環境差分と準備状態を recent/timeline/compare/live preview で確認でき、secret 実値・外部URL・Slack IDを保存しない。
- accept（後段）：通話横断で active_tasks 単調増加が検出・可視化される。

**Phase 5 — 抽象の実証＋スケール**
- 2つ目のプロバイダ（OpenAI Realtime）と2つ目のエンジンをプラグインで追加し、コア無改修で動くことを実証。任意でHEP/RTP取込、`--profile scale`。
- accept：コア（control-plane/core, engine-harness/core）に差分ゼロで新プロバイダが動く。

---

## 15. コーディングエージェントへの指示

- **§2の非目標を絶対に破らない。** 特に「特定値ハードコード禁止」「PipeCat再実装禁止」「コアにプロバイダ固有ロジック禁止」。
- Kaikura由来の事例値（8kHz, target_rms=3000, CLEAR_STREAM_AFTER_SECS, silence_ms=600 等）は `examples/` のサンプルにのみ書く。コア・プラグイン本体には書かない。
- 新しい劣化パターンに出会ったら、コードを増やす前に「新しい invariant か / 新しい requires_host_capability か / 新しい metric か」を先に問う。設計の拡張点は manifest 側にある。
- 各プラグインは manifest を最初に書き、その契約に実装を従わせる（contract-first）。
- フェーズは飛ばさない。Phase 0 の検証基盤が無いまま Phase 1 のコールを実装しない（再現性が壊れる）。
- 不確実な箇所は実装を進める前に設計者に確認する論点として残す（特に turn_taking の検証ルール、ViSQOL のステージ別参照の取り方）。

---

## 付録：未確定の論点（実装中に詰める）
- turn_taking 検証の hard-fail/warn の線引き（プロバイダごとに差が出る可能性）。
- ステージ別参照音声の生成方法（各ステージの期待フォーマットへの“正しい”帯域制限の実装）。
- `replay` シナリオでの参照音声の同期・整列（実コール録音をどう基準長に揃えるか）。
- scaleプロファイルでの `metrics`/`spans` 移送のトリガと保持期間。
