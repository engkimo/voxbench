# 進捗

## Product UX: persisted barge-in causal evidence (2026-07-23)

- observer batchにsafe typed timeline eventを追加し、`timeline_events` table / migration
  `0009_timeline_events`へevent ID、category、clock、direction、source、correlation alias、scalar attributesを永続化。
- AudioSocketのbarge-inを、provider speech/interrupted通知、auto/request interrupt、truncate位置、playback queue
  clear、完了の因果列として観測。provider item IDやraw payloadは保存しない。
- 同じ相関aliasからconversation intervalと`Barge-in handled` incidentを導出。処理時間、interrupt経路、
  played audio end、破棄frame/queue時間をObserved evidenceへ出し、未計測の可聴テールとは明確に区別する。
- Call inspectorのincident drawerに時刻付きevidence chainを追加。各stepを押すと共通cursorと音声位置が移動する。
- scalar barge-in metricは互換seriesとして維持し、typed eventがあるrunでは重複event markerを抑止する。
- 次はdirectional media gap/burst loss、Stage間media-time contractionとgain/loudness、実音声でのbarge-in
  usability検証。製品全体は約75%。

## Product UX: typed common-time-axis call inspector (2026-07-23)

- 既存timelineを壊さず、`events` / `intervals` / `series` / `artifacts` / `incidents`のtyped projectionを追加。
  各要素はcategory、相対ms、clock domain、direction/stage/stream、sourceを表現できる。
- SIPをsignaling event、RTP jitter/loss/MOS/RTTをdirection別transport series、Stage/host/provider metricsを
  series、OTel spanをinterval、WAVをaudio artifact、verification failureをevidence付きincidentへ投影する。
- raw Call-ID、span attrs、storage URIを新projectionへ複製しない。artifactはsafe `recording:<stage>`参照とformatだけを持つ。
- WebにCall inspectorを追加。1本のruler/cursorでConversation、Signaling、Transport、Buffers、Pipeline、
  Provider、Host、Sessionを結び、incident選択、Observed/Expected drawer、cursor位置からのStage音声再生を実装。
- `?run_id=...&compare_run_id=...`のdeep linkを追加し、browser storageを使わずrun選択を共有・再現できる。
- Docker Postgres上の3秒failure runでAGC incident、SIP marker、方向別RTP points、audio artifactsを画面確認。
  Web build、full suite 288 passed/3 skipped、real Postgres 3 passed、Ruff、npm audit high以上0件。
- 製品全体は約73%。次はtyped barge-in causal sequence、directional media-gap/burst-loss事件、Stage間の
  media-time contraction/gain series、AudioSocket/chan_websocket semantic decoder、clock alignment confidence。

## Product Web UX: audible quick demo and visible refresh (2026-07-22)

- 画面先頭に3秒の`Run audible demo`を追加。API key/Asterisk不要の非無音合成toneを実行し、完了runを
  自動選択して4 stageの録音を画面上部で再生できる。target loudnessは0 / +2.5 / +6 dBから選択可能。
- 録音を右rail最下部から主column先頭へ、選択stage詳細をstage一覧直下へ移動。stage click時は詳細へscrollし、
  metric未取得stageを誤って`pass`表示せず`not measured`とする。
- same-runの`Fetch`でもtimelineを明示再取得。run/live-previewを2秒pollし、WebSocket error/close時には古い
  snapshotを破棄してRESTへfallbackする。Vite `/api` proxyにもWebSocket転送を有効化した。
- readinessのunknownをfailureと同一視せず`not checked`として表示。Recent runsを常時見える位置へ置き、
  custom runとadvanced diagnosticsは初回操作を妨げない折り畳みにした。
- Docker Postgres 16 + Alembic `0008_run_job_leases` + 実API上でquick demoの完了、4本×3秒の非無音WAV、
  AGC +2.499 dB、0 violations、WebSocket snapshot、Web UI自動選択/再生導線を確認した。
- backend/deployment基盤は約99.5%だが、製品UXを含む全体は約70%。残りの主な製品課題は、実音声uploadまたは
  同梱speech sample、同期A/B再生、処理差の説明、初回ガイド、実通話を含むend-to-end usability検証。

## Phase 1/5 real Postgres deployment validation (2026-07-22)

- Homebrew PostgreSQL 14を14.23へpatch更新し、削除済みICU 74参照を現行ICU 78へ修復。既存data directoryや
  serviceは使わず、使い捨てcluster・DB・portでproduction相当のmigration/API smokeを実走した。
- fresh PostgresでAlembic標準`alembic_version.version_num VARCHAR(32)`を超える0004 revision IDが
  migrationをrollbackする問題を検出。0004内でversion列を128文字へ拡張してからrevision更新するよう修正。
- real Postgres integration fixtureを`Base.metadata.create_all()`からfull Alembic upgradeへ切替え、migration
  history/headも継続検証するようにした。
- spanのepoch nanosecondsをORMが32-bit `INTEGER` parameterとしてcastし、結果commitをoverflowさせる問題を
  検出。modelを明示`BigInteger`へ合わせ、10^18台のstart/end ns round-trip testを追加した。
- full migration、`SKIP LOCKED`、expired lease fencing、readiness probe、statement timeout、persistent
  `/runs/async` workerとrecording生成を実Postgres 14.23上で確認。残りはmulti-process soak、shutdown/alertの
  deployment運用接続。全体進捗目安は約99.5%。

## Phase 1/5 Postgres worker production-readiness contracts (2026-07-22)

- `VOXBENCH_POSTGRES_STATEMENT_TIMEOUT_MS`を100〜30,000ms、既定5,000msで追加し、production
  psycopg sessionへlibpq `options=-c statement_timeout=...`として適用。connect timeoutとは責務分離する。
- supervisorにprocess-local processed/error/lease-lost counter snapshotを追加。repository readinessへ
  enabled/runningとcounter、statement timeoutだけを投影し、worker/job/DB identity、token、error detailは除外。
- `VOXBENCH_TEST_POSTGRES_URL`指定時だけ動くreal Postgres testを追加。random unique schemaをcreate/dropし、
  held rowを待たず次jobをclaimする`SKIP LOCKED`とexpired lease後のstale result拒否を直接検証する。
- 通常suiteでは実Postgres 2件を明示skip。ローカルPostgres 14 binaryは削除済みICU 74へのlink切れで
  起動不能だったため、package再導入は行わず外部test URL待ちとした。
- 全体進捗目安は約99%。残りは実Postgres URL上でのopt-in test実走、deployment migration、worker
  shutdown/timeout alertの運用接続、実multi-process soak test。

## Phase 1/5 persistent Postgres async worker lifecycle (2026-07-21)

- `PostgresRunRepository.save_queued_run(...)`でinitial runとqueued jobを同一transactionに保存し、
  save成功/enqueue失敗でorphan running runだけが残るcrash windowを除去。idempotent job参照も維持する。
- `RunWorkerSupervisor`を追加。idle 0.05〜10秒、error 0.1〜60秒、shutdown 0.1〜30秒のbounded wait、
  idempotent start/stop/restart、exception後backoff、lease lost後backoff、queue drainを実装した。
- FastAPI推奨lifespan contextでPostgres workerをprocessごとにstartupし、shutdown時にEvent通知＋bounded join。
  worker/threadをmodule import時には開始しない。
- Postgres `/runs/async`をatomic persistent submitへ切替。memory modeは既存process-local daemon互換を維持。
- queued jobとexpired leaseはstartup pollingで自然回収。expired leaseをattempt 2で完了するrestart test、
  lifespan前後のworker状態、atomic rollback、retrying supervisor、shutdown timeoutを固定した。
- queue/fencingは複数Postgres process向けだが、real Postgres concurrent test、statement timeout、shutdown
  telemetry、deployment migration検証をproduction scale前の残作業とする。全体進捗目安は約98%。

## Phase 1/5 bounded Postgres single-job worker (2026-07-21)

- `RunJobWorker.run_one()`を追加し、1 jobのclaim、run load、実行、retry/finalizeをpolling lifecycleから分離。
- harness実行中は1〜lease半分秒間隔のdaemon heartbeatを維持し、`Event`停止後に最大1秒だけjoinする。
  heartbeat拒否・queue error・join timeoutはlease lostとして結果/failureを書かない。
- claimに`final_attempt`を追加。途中attemptの実行例外は固定`engine-harness-error`で0〜3,600秒のretry、
  final attemptはfailed runとfailed jobを既存fenced transactionでatomic commitする。
- run欠落はorphan jobを固定`run-not-found`でterminal fail。worker resultはjob/run/attempt/outcomeだけで
  opaque tokenや例外detailを返さない。
- harness結果生成を通常repository saveから分離し、次の永続worker接続時にunfenced saveを再利用しない。
- heartbeat継続、成功commit、retry→final fail、lease loss結果破棄、run欠落、全timing boundをtest固定。
  全体進捗目安は約97%。次はbounded polling lifecycle、shutdown、restart recovery、`/runs/async`切替。

## Phase 1/5 fenced Postgres run-result commit (2026-07-21)

- `PostgresRunRepository.commit_leased_result(...)`を追加し、terminalなrun結果だけをfenced commitする。
- matching `run_jobs` rowを`FOR UPDATE`し、run ID・job ID・worker alias・opaque lease token・未期限を
  同じtransaction内で再検証。stale/expired/mismatched workerはrunを変更せずfalseで拒否する。
- run本体とrecording/span/metric/verification/SIP/RTPの正規化child replace、jobの
  `completed|failed`化、lease credential消去を1 transactionに統合した。
- DB flush失敗時にrun結果とjob stateの両方がrollbackされるtest、expired lease再claim後に旧workerが
  overwriteできないtest、completed/failed terminal stateの統合testを固定した。
- 現行`/runs/async`はまだprocess-local daemon runnerのまま。bounded worker lifecycle、heartbeat、
  retry分類、restart recoveryを実装してから永続queueへ切り替える。全体進捗目安は約96%。

## Phase 1/5 Postgres run job lease foundation (2026-07-21)

- migration `0008_run_job_leases` と `run_jobs` modelを追加し、runごとのidempotent enqueue、
  `queued|leased|completed|failed` state、available time、attempt、safe failure aliasを永続化する。
- claimはSQLAlchemy `with_for_update(skip_locked=True)`からPostgres `FOR UPDATE SKIP LOCKED`を生成し、
  5〜300秒のbounded lease、opaque lease token、worker alias、attempt増分を1 transactionで更新する。
- heartbeat/complete/failはjob・worker・lease token・未期限を完全一致させ、stale workerを拒否する。
  expired leaseは新tokenで再claim可能、retry delayは0〜3,600秒、attempt上限到達時はterminal fail。
- Postgres runtimeへqueueをsecret-free injectionし、`GET /repository/readiness`に
  `job_queue_enabled`だけを追加。memory modeはfalse、Postgres modeはtrue。
- expected migration headを `0008_run_job_leases`へ更新した。現行async runnerへはまだ接続せず、
  run result commit側のfencingなしに複数workerを動かさない安全境界を維持する。
- SQLite state-machine testとPostgres dialect compile testでlease/reclaim/stale rejection/retry/limit/
  `SKIP LOCKED`を固定した。全体進捗目安は約95%。次はfenced run commit＋worker loop＋restart recovery。

## Phase 1/5 bounded Postgres readiness and safe failure boundary (2026-07-21)

- `VOXBENCH_POSTGRES_PROBE=true` の明示時だけstartupで固定 `SELECT 1` と
  `alembic_version`を1回確認し、configured expected migration head完全一致時だけ`ready`を返す。
- probe timeoutは `VOXBENCH_POSTGRES_PROBE_TIMEOUT_MS` で10〜10,000ms、既定2,000ms。daemon workerを
  bounded joinし、接続/query失敗、migration不一致、timeoutをraw errorなしの固定aliasへ変換する。
- probe既定offは従来どおり`configured/connectivity-and-migrations-not-checked`でnetworkアクセスしない。
  成功/不一致/例外sanitization/10ms timeout/invalid flag・timeoutをtest固定した。
- `PostgresRunRepository`のSQLAlchemy例外を `RunRepositoryError` へ統一し、FastAPI境界で固定503と
  `Retry-After: 1`へ変換。SQL、driver message、DB URL、query parameterをresponseへ返さない。
- 自動retryは行わず、mutation再試行の判断をcallerへ残す。全体進捗目安は約94%。次は
  Postgres-backed job lease、またはappend/idempotency/lockingを持つtelemetry persistence最適化。

## Phase 1/5 opt-in Postgres run persistence (2026-07-20)

- `RunRepository` protocolを追加し、既定のprocess-local memory互換を維持したまま、SQLAlchemy 2.xの
  short transactionでrun stateをPostgresへ保存・再構築する `PostgresRunRepository` を追加した。
- run本体にresolved config/failure aliasを保存し、recording/span/metric/verification/SIP/RTPを既存の
  正規化tableへatomic replaceする。各childへordinalを追加し、restart後もAPI配列順を決定的に復元する。
- synchronous/background harness、simulated live、observation batch、個別SIP/RTP、complete/failの全mutation
  後に明示saveするよう修正し、in-memoryの参照共有に依存していた暗黙永続化を除去した。
- `VOXBENCH_RUN_REPOSITORY=postgres` とprocess-only `VOXBENCH_DATABASE_URL`によるopt-in startupを追加。
  URLは明示 `postgresql+psycopg`だけ許可し、readiness/reprへhost/user/passwordを出さない。
- startupはDB接続/migrationを暗黙実行せず、`GET /repository/readiness` はPostgresを
  `configured/connectivity-and-migrations-not-checked`と正直に返す。Alembicも同じenv URLを使用する。
- migration `0007_run_runtime_state`、optional `.[postgres]`、SQLite ORM restart integrationを追加した。
  全体進捗目安は約93%。次はbounded Postgres readiness probe、DB errorのsafe 503 mapping、または
  Postgres-backed job leaseへ進む。

## Phase 1/5 Web remote-audio session boundary (2026-07-20)

- process-level storage Bearerをfrontendへ渡さず、別のoperator login tokenを短命な署名cookieへ交換する
  opt-in Web session境界を追加した。sessionはremote proxy有効時だけ起動できる。
- login tokenとHMAC-SHA256 signing secretは32〜256文字のASCII・空白なし・相互に別値を必須化し、
  `repr`、readiness、responseから除外した。cookie TTLは60〜3,600秒、既定900秒。
- cookieは `HttpOnly`、`SameSite=Strict`、`Secure`既定true、path `/`。改ざん、期限切れ、未来すぎる
  expiryを拒否し、logoutで削除する。login bodyは1 KiBに制限し、失敗responseへ入力値を反射しない。
- audio endpointは従来のserver-to-server Bearerまたは有効session cookieを受け付け、local playbackと
  session/proxy既定off互換を維持する。readinessはenabled/Secure/TTLというsafe capabilityだけを返す。
- Webに一時password入力、lock/unlock/status/期限表示を追加した。成功時に入力を消去し、永続保存せず、
  WaveSurfer/native audioのcredential付き再取得をsession revisionで明示的に更新する。
- auth cookie/env/startup sanitization/body bound/remote playback/logoutをbackend testで固定し、Web buildで
  credential fetch contractを検証した。全体進捗目安は約90%。次はPostgres永続化連携、またはsessionの
  revocation/auditと実HTTPS同一origin deployment検証。

## Phase 1/5 authenticated bounded remote audio proxy (2026-07-20)

- MinIO `get_object`をserver-sideだけで使う `MinioRecordingReader` を追加し、presigned URLや
  storage credentialをbrowser/responseへ返さず既存audio endpointからWAVを返せるようにした。
- remote proxyは `VOXBENCH_REMOTE_AUDIO_PROXY=true` の明示時だけ有効。32〜256文字のprocess-level
  Bearer tokenを必須とし、constant-time比較、不正/missing tokenは固定401で拒否する。
- 保存済み `s3://` URIがconfigured bucket/prefixとrequest run/stageから導くobject keyに完全一致する
  場合だけ読込む。query/fragment、別bucket/prefix/run/stage、非S3 URIはnetwork前に拒否する。
- byte上限44〜64 MiB、同時read 1〜8、connect/read timeout 100〜30,000msをbounded設定化した。
  総in-flight payloadは128 MiB以下とし、SDKにもlimit+1 byteだけ要求。oversize、非RIFF/WAVE、
  busy、timeout、SDK失敗をsafe HTTPへ変換する。
- custom urllib3 clientはTLS証明書検証＋certifi CAを明示し、自動retryなし。responseは必ずcloseする。
- local playbackとproxy既定offの404互換を維持し、readinessにはenabled booleanだけを追加した。
- identity/limit/content/error cleanup/deadline/concurrency/auth/upload→read統合/startup secret sanitizationを
  test固定した。全体進捗目安は約88%。次はWeb向けsession auth/BFF、またはPostgres永続化連携。

## Phase 1/5 bounded MinIO bucket readiness probe (2026-07-18)

- `VOXBENCH_MINIO_PROBE_BUCKET=true` の明示時だけ、process startupでbucket existenceを1回確認する
  opt-in probeを追加した。既定falseではnetwork requestを行わず `configured` を維持する。
- `VOXBENCH_MINIO_PROBE_TIMEOUT_MS` を10〜10,000msに制限し、daemon workerの待機をboundedにした。
- bucket存在時は `ready`、bucket無しは `unavailable/bucket-not-found`、SDK例外は
  `unavailable/bucket-probe-failed`、timeoutは `unavailable/bucket-probe-timeout` とする。
- raw SDK message、endpoint、credentialをreadinessへ入れず、probe失敗ではstartupを落とさない。
- bucket auto-create/retryは行わず、provisioning責務をruntimeから分離したままにする。
- success/missing/error sanitization/timeout/invalid probe configをtest固定した。
  全体進捗目安は約86%。次はauthenticated remote audio proxy、またはproduction persistence連携。

## Phase 1/5 environment-driven recording storage startup (2026-07-18)

- `create_app_from_env(...)` とmodule-level ASGI appを追加し、録音sinkをprocess environmentの
  `local|minio` 選択から構築できるようにした。既定値は従来どおりlocal。
- MinIO endpoint/access key/secret key/bucketはdeployment environmentからだけ読み、run payload、
  repository、artifact URI、readiness responseへcredentialを渡さない。
- endpoint、TLS flag、必須env、bucket/prefixをfail-fast検証し、client生成を含む全エラーを
  固定safe reason aliasへ変換する。生の設定値や外部例外messageは返さない。
- `GET /storage/readiness` は `local=ready`、`minio|injected=configured` を返す。
  MinIOではsafe bucket/prefix aliasとTLS選択だけを公開し、endpoint/credentialはmodelにも持たない。
- `configured` はnetwork/bucket probe未実施を明示する状態であり、疎通済みとは扱わない。
  bucket provisioningも引き続きdeployment責務。
- default/MinIO/error sanitization/factory failure/readiness API/injected sinkをtest固定した。
  全体進捗目安は約85%。次はbounded bucket readiness probe、またはauthenticated remote audio proxy。

## Phase 1/5 Control Plane recording sink injection (2026-07-18)

- `RunApiState` と `create_app(...)` にoptional `RecordingSink` injectionを追加した。
  未指定時は従来どおりlocal sinkを使うため既存API/test互換を維持する。
- MinIO client/credentialはdeployment startup側だけに保持し、run request payload、stored run、
  response/timelineへ渡さず、recordingにはcredential-free `s3://` URIだけを保存する。
- injected MinIO sinkで `POST /runs` の4 stage uploadとresponse URIを統合testした。
- local-only audio endpointはremote artifactにpresigned URLを返さず404を維持する。
  authenticated object read/proxyが実装されるまで安全側へ倒す。
- 全体進捗目安は約84%。次はenv-driven startup factoryとbucket readiness、またはremote proxy。

## Phase 1/5 MinIO recording sink boundary (2026-07-18)

- 既存 `RecordingSink` protocolへ適合する `MinioRecordingSink` と
  official minio-py `fput_object`互換client protocolを追加した。
- stage WAVをbounded temporary directoryへ生成し、`audio/wav`としてobject storeへupload、
  完了/例外後にlocal temporary artifactを削除する。
- artifact URIは `s3://bucket/prefix/run/stage.wav` のみ。endpoint、access key、secret、
  upload responseは保存しない。
- DNS-style bucket、IP形式拒否、safe prefix/run/stage segmentを検証し、`../`、slash、URL等の
  object-key injectionを拒否する。bucket作成はruntime sinkが暗黙実行せずprovisioning責務。
- optional `.[storage]` に公式MinIO Python SDK 7.2系を追加した。
- fake clientでWAV header/frame、upload args、temp cleanup、credential非混入、unsafe keyをtest。
  全体進捗目安は約83%。Control Plane選択/remote取得と実MinIO統合は次slice。

## Phase 2 ViSQOL repeatability calibration report (2026-07-18)

- 複数のbaseline treatment aggregateからrepeatabilityを記述する
  `analyze_full_reference_repeatability(...)` を追加した。
- 3 repeat以上かつ同一contract/stage/transformation、全入力aggregatedの場合だけ、
  mean-of-means、min/max mean、observed max pairwise delta、population stddevを返す。
- repeat不足、stage欠落、未集約、contract/transform mismatchは統計を抑止し
  safe reason付き `indeterminate` とする。
- `voxbench visqol-calibrate-repeatability` を追加。report pathを出力せず、観測値のみを返す。
  自動toleranceや統計的有意差は生成しない。
- 完全/不足/欠落/未集約/変換・contract不一致とCLI exitをtest固定した。
  全体進捗目安は約82%、Phase 2は約98%。

## Phase 2 Persisted treatment comparison CLI (2026-07-17)

- treatment reportにscorer score rangeを追加し、bounded strict loaderとwriterを追加した。
- loaderはstandalone aggregateとsynthetic treatment wrapperの両方を読み、1 MB上限、
  alias、state、finite stats、count合計、transformations、contractを検証する。
- `voxbench visqol-compare-treatments` を追加し、persist済みbaseline/currentを明示toleranceと
  metric方向で比較する。regressionあり=1、indeterminateあり=2、それ以外=0。
- CLI JSONはreport pathを含まず、invalid/unsafe reportのraw内容も表示しない。
- round-trip、count不整合、wrapper読込、regression/indeterminate exitをtest固定した。
  全体進捗目安は約81%、Phase 2は約96%。

## Phase 2 Treatment regression policy (2026-07-17)

- aggregate済みbaseline/currentを比較する `compare_full_reference_treatments(...)` と
  明示的 `FullReferenceRegressionPolicy` を追加した。
- finite/non-negativeなstable toleranceとhigher/lower-is-better方向をcallerが指定する。
  toleranceを暗黙の統計的有意差として扱わない。
- 両側がaggregated、同一scorer contract、同一stage transformation chainの場合だけ
  `improved/stable/regressed` を返す。
- missing stage、partial/insufficient aggregate、contract/transform mismatch、mean欠落は
  score差を判定せずsafe reason付き `indeterminate` とする。
- 境界、方向反転、各indeterminate reason、contract mismatch、不正toleranceをtest固定した。
  全体進捗目安は約80%、Phase 2は約94%。

## Phase 2 Synthetic ViSQOL treatment CLI (2026-07-17)

- `run_synthetic_treatment(...)` と `voxbench synthetic-visqol-treatment` を追加した。
- 同一config/scorer treatmentのままsource frequencyをsampleごとに変え、異なるcontentの
  `sample-NNN` artifactと個別 `verification-report.json` を生成する。
- 全sampleのstage scoreを既存aggregation contractでまとめ、path-free
  `treatment-report.json` にsample stateとstage統計を保存する。
- 全stage aggregatedのみcomplete/exit 0。minimum不足・sample partial・aggregate不完全は
  partial/exit 2、sample failureはfailed/exit 1とする。
- 3 sample × 4 stageのfake executable統合test、個別/aggregate report永続化、mean、
  minimum不足時の統計抑止を固定した。全体進捗目安は約79%、Phase 2は約91%。

## Phase 2 Full-reference treatment aggregation (2026-07-17)

- `aggregate_full_reference_reports(...)` を追加し、同一treatment/scorer contractの
  stage scoreを複数sampleで集約できるようにした。treatmentはsafe aliasに限定する。
- 既定3件を満たすまでmean/median/min/max/population stddevを公開せず、単一MOS-LQOを
  regression判定へ誤用しにくくした。
- 状態は `aggregated/insufficient/partial/incomparable`。欠測・block・unavailable・failed
  混在をpartial、reference/scorer transformation chain不一致をincomparableとする。
- scorer contract混在、同report内のstage重複はhard error。safe payloadはcount、統計値、
  transformationだけでsample IDやartifact pathを持たない。
- 3 sample統計、minimum不足、unavailable混在、missing stage、変換不一致、contract/duplicate
  rejectionをtestで固定した。全体進捗目安は約78%、Phase 2は約88%。

## Phase 2 Synthetic full-reference orchestration (2026-07-17)

- `run_synthetic_verification(...)` でconfigに対するartifact生成、signal invariant検証、
  full-reference candidate選択、全stage scoring、score metric結合を1つにつないだ。
- run stateは `complete/partial/failed`。invariant failureまたはscorer failureはfailed、
  binary unavailable/input blockはpartialとし、未評価をpass扱いしない。
- safe reportはinvariant observation、score、safe reason、reference生成からscorer入力までの
  transformation chainを保持し、artifact URI、absolute path、config secret reference、
  process outputは含めない。
- `voxbench synthetic-visqol` を追加し、既定5秒のstage artifactと
  `verification-report.json` を生成する。complete=0、failed=1、partial=2。
- fake executableを実processとして4 stageで起動するCLI統合testを追加し、score、metric、
  μ-law/reference/ViSQOL resample履歴、missing binary時のpartial report永続化を固定した。
- 全体acceptance進捗の目安は約77%、Phase 2は約86%。次は複数sample/treatmentのscore集約と
  実official binaryによる校正。

## Phase 2 ViSQOL score CLI (2026-07-17)

- matching local mono PCM16 WAV pairをoptional official ViSQOL binaryで評価する
  `voxbench visqol-score` を追加した。
- WAV headerからrateを取得し、reference/degradedのencoding/rate/channels一致を
  process実行前に検証する。speech/audioのscorer入力変換はadapterと共有する。
- stdoutはscore/state/safe reason/transformationsだけのpath-free JSONとし、成功0、
  scorer failure 1、binary unavailableまたはCLI input error 2のexit codeを返す。
- real subprocessを使うfake binary CLI testで8→16 kHz score、missing binary、
  mismatched WAV rejectionをend-to-end確認した。

## Phase 2 Optional ViSQOL CLI adapter (2026-07-17)

- official ViSQOL binaryを明示pathで利用するoptional `VisqolCliScorer` を追加した。
  package/binary/modelはcore dependencyに含めず、未導入時は `unavailable` とする。
- official contractに合わせ、speech modeではreference/degraded両方を16 kHz、audio
  modeでは両方を48 kHzへ同一resampleし、一時mono PCM16 WAVとして実行する。
- stage-native artifactは上書きせず、一時directoryはstage score後に削除する。
  modeとresample履歴はscore resultへ保持し、8 kHz stageの帯域制限由来を監査可能にした。
- subprocessはshellを使わず、stdout/stderrを破棄する。binary/path/processのraw errorは
  reportへ保存せず、既存scorer boundaryでsafe stateへ正規化する。
- fake CLI testでspeech/audio mode、8→16 kHz両側変換、temp cleanup、binary欠落、
  WAV format mismatch、process failure秘匿を固定した。実ViSQOL binaryでのscore校正は
  environment validationとして残る。
- 全体acceptance進捗の目安は約76%、Phase 2は約82%。実Asterisk/provider通話と
  production保存経路は引き続き全体進捗の主要gap。

## Phase 2 Full-reference scorer execution contract (2026-07-17)

- optional full-reference scorer向けに、safe scorer/metric alias、score range、readiness、
  stage単位の実行protocolをverification public APIへ追加した。
- 結果は `scored/unavailable/blocked/failed` を区別し、dependency未導入、入力block、
  scorer例外を偽の0点やpassへ変換しない。
- scorer例外のraw message、path、URL、tokenは保持せずsafe reason aliasへ正規化する。
  unsafeなreadiness/block reasonも固定fallbackへ置換する。
- 1 stageの失敗後も後続stageをscoreし、finiteかつ宣言range内の成功値だけを
  `MetricArtifact` へ変換する。NaN、infinity、range外はmetric化しない。
- fake scorer testで成功/失敗分離、optional dependency未導入、例外秘匿、range、
  metric timestampを固定した。ViSQOL本体adapterと48/16 kHz入力変換は次slice。

## Phase 2 Full-reference scorer input gate (2026-07-16)

- `select_full_reference_candidates(...)` をverification public APIへ追加した。
- `comparison_ready` なstage referenceと同stage recordingをpairにし、decoded
  `encoding/rate/channels` が一致する候補だけをfuture scorerへ渡す。
- unsupported codec、recording欠落、duplicate reference、format mismatchは
  scorerを呼ばずsafe block reasonへ正規化する。
- Synthetic stage recordingもresolved output rate/channelsで生成し、8 kHz μ-lawは
  round-trip後のdecoded PCMを保存してreferenceと比較条件を揃えた。
- candidate/block ordering、μ-law transformation、各block reasonをtestで固定した。

## Phase 2 G.711 mu-law reference round-trip (2026-07-16)

- dependency-freeのG.711 μ-law sample/buffer encode/decode helperを追加した。
- 8 kHz μ-law stage referenceはPCM16 toneを8-bit μ-lawへencodeし、PCM16へdecodeした
  実quantized waveformを保存する。
- serializer stageの `comparison_ready` をtrueへ更新し、codec変換履歴は保持する。
- known zero/full-scale code、全256 code decode/re-encode、buffer sample count、
  partial PCM rejectionをtestで固定した。negative zero codeはcanonical zeroへ正規化する。
- 未対応codecと8 kHz以外のμ-lawは引き続きcomparison blockとなる。

## Phase 2 Stage-specific reference audio contract (2026-07-16)

- Synthetic Callerの単一clean referenceに加え、resolved pipelineの各stageごとに
  expected rate/channelsのreference WAVとformat metadataを生成する。
- artifactはstage format、PCM comparison format、変換履歴、`comparison_ready`、
  `blocked_reason` を保持し、future ViSQOL/PESQ scorerの入力gateにできる。
- PCM16/linear16 stageは比較可能。μ-law等のcodec stageは実codec round-trip未実装のため
  `codec-round-trip-required:<encoding>` で明示的にblockする。
- source/target Nyquistを満たさないsynthetic toneを拒否し、aliasingした参照生成を防ぐ。
- baseline testで8 kHz stage reference、変換履歴、WAV format、μ-law blockを確認した。

## Phase 4 Cross-session resource trend (2026-07-16)

- DESIGN記載の `GET /runs/cross-session-trends` を実装した。
- 同一 `server_alias` の終了済みrunだけを時系列に並べ、各runの最新
  `active_tasks` / `memory_rss_bytes` を比較する。running runやaliasなしは除外する。
- 3 run以上で全区間が非減少かつ総増分が正の場合だけ `increasing` と判定し、
  それ以外を `stable`、3件未満を `insufficient` とする。
- Web Live previewは5秒pollでtrendを表示し、increasingを赤、stableを緑で可視化する。
- API testで最新run値の選択、単調増加、下降を含むstable、server分離、
  running/aliasなし除外、minimum sampleを固定した。
- このsliceによりPhase 4後段のcross-session acceptanceを満たし、全体進捗目安は
  約72%から約74%、Phase 4は約75%から約85%へ更新する。

## Overall progress checkpoint (2026-07-16)

- DESIGN Phase 0–5のacceptanceと現実装を照合した全体進捗は約72%。
- 現在の主目的であるlocal realtime voice demoに限定した完成度は約85%。
- phase別目安は Phase 0: 100%、Phase 1: 75%、Phase 2: 70%、Phase 3: 90%、
  Phase 4: 75%、Phase 5: 25%。数値はcommit数ではなくacceptance充足度の評価。
- 主な残件は、実Asterisk/softphone/provider通話検証、cross-session trend、
  ViSQOL/PESQと参照音声生成、Postgres/MinIO/OTLPのproduction保存経路、
  2つ目のengineとscale profile。
- 実機依存作業を待つ間は、Phase 4後段acceptanceのcross-sessionリーク検出を次に進める。

## Asterisk RTCP collector operator status (2026-07-16)

- AMI認証成功、RTCP event送信、collector failureをhost-level operational metricで記録する。
- Live preview responseに `rtp_collector` projectionを追加し、状態を
  `inactive/connected/collecting/failed` に正規化した。
- event/failureはdelta metricをrun全体で加算するため、同じrunでcollectorを再起動しても
  countが巻き戻らない。
- WebのLive previewに専用RTP collector blockを追加し、generic host metric tileから
  collector metricを分離した。
- fake AMI統合testとControl Plane projection testでconnected/event/failure遷移を確認した。

## Asterisk AMI RTCP collector (2026-07-16)

- reporting-only AMI accountから `RTCPReceived` / `RTCPSent` だけを読み取る
  `voxbench asterisk-ami-rtcp` CLIを追加した。
- RTCP report blockのloss fixed fractionを百分率へ、interarrival jitterを明示した
  RTP clock rateからmsへ、received RTTを秒からmsへ変換する。
- 複数report blockは最大値で保守的に集約し、方向とRTTをRTP timeline schema/UIへ追加した。
- Channel、caller ID、address、SSRC、raw SIP/SDP/packet、AMI secretはControl Planeへ
  転送しない。MESから根拠のないMOS変換も行わない。
- bounded AMI parser、fake local AMI server、単位変換・異常値・機密field非混入をtestで固定した。
- 実Asterisk/softphone callでのRTCP値とcodec clock rateの確認は次の環境検証項目。

## Mid-call provider stream failure hardening (2026-07-15)

- OpenAI/Geminiのreceive streamをpersistent contractとして宣言し、通話中のclean EOFを
  completed扱いせず `provider-stream-ended` でrun failureにする。
- provider receive例外はraw message/URL/responseを保存せず、
  `provider-session-error` と `provider_stream_errors` metricへ正規化する。
- AudioSocket入力待ち中にoutput taskが終了していた場合もfinallyで結果を回収し、
  terminateとのraceで誤ってrun completeにならないようにした。
- finiteなdry-run/fake providerはpersistentではないため、既存embedding testとの互換を維持する。

## Provider connection operator panel (2026-07-15)

- Live preview responseに `provider_connection` projectionを追加し、接続metricを
  `pending/connected/exhausted/unobserved/not_applicable` に正規化した。
- attempt/retry/failure/exhaustion countを専用blockに表示し、generic host metric
  tilesからprovider connection metricを分離した。
- 接続成功metricはprovider session確立直後にflushするため、通話中のWebSocket
  snapshotから確認できる。failed runとexhausted connectionは赤い状態で強調する。
- API testでpending、retry後connected、attempt枯渇exhaustedを確認した。

## Initial provider connection retry hardening (2026-07-15)

- `audiosocket-realtime` は observed run を先に作成し、provider session の初回接続を
  既定3回、bounded exponential backoff付きで再試行する。
- `--connect-attempts` と `--connect-backoff-seconds` でdemo環境のretryを調整できる。
- attempt/retry/failure/exhaustionをhost metricへ記録し、枯渇時はraw provider errorを
  保存せず `provider-connect-error` でrunをfailedにする。
- 確立済み通話の透過reconnectはconversation stateを失うため実装していない。
  mid-call disconnectは明示的なrun failureとして扱う。

## OpenAI Realtime playback truncation hardening (2026-07-15)

- OpenAI `response.output_audio.delta` の `item_id` / `content_index` を provider
  audio chunk に保持し、AudioSocket の20 ms pacingに対応する再生位置を追跡する。
- `input_audio_buffer.speech_started` では local queue を破棄し、最後にcallerへ
  送った位置の `audio_end_ms` で `conversation.item.truncate` を送る。
- OpenAI server VAD が response を自動cancelする契約に合わせ、bridgeからの重複
  `response.cancel` は避ける。明示cancelしか持たないprovider境界との互換は維持する。
- `provider_auto_interrupts` / `provider_truncate_requests` を operational metric
  として記録し、provider itemを跨ぐときはpacket/resampler stateを切り替える。
- fake providerによる位置・truncate検証まで完了。実provider/Asterisk通話での
  truncation timingと音切れ確認は引き続き環境検証項目。

## Live softphone realtime demo hardening (2026-07-14)

- Asterisk AudioSocket から OpenAI Realtime / Gemini Live へ接続する provider
  bridge と、direct-provider / Pipecat 向けの `voxbench.observability` 境界を実装した。
- PCM16 mono の resampler は chunk 間で位相を保持する streaming 実装に更新した。
- provider の speech/response/interruption event を正規化し、barge-in 時に
  local playback queue を破棄して `barge_in_events` / `output_frames_dropped` を記録する。
- OpenAI はactive responseだけに `response.cancel` を送り、
  `provider_interrupt_requests` を記録する。再生済み位置に合わせたitem truncateは次段。
- observed run に `POST /runs/{run_id}/fail` と安全な `failure_alias` を追加し、
  Live preview で失敗理由aliasを確認できるようにした。
- observed run -> localhost TCP AudioSocket -> fake provider -> stage WAV / metrics
  -> Control Plane timeline -> run complete の統合テストを追加した。
- 未実施なのは実API keyを使ったprovider接続と、実Asterisk/macOS softphoneでの通話確認。
  次は実機でlatency/audio qualityを測定し、provider側cancel/truncate、RTP stats収集、
  reconnectを詰める。

## Phase 0

- §2 と §15 を確認し、Phase 0 のみに限定して着手。
- 具体値は `examples/` のサンプルデータに限定する方針。
- turn_taking の hard-fail/warn 境界は付録で未確定のため、Phase 0 acceptance の対象ルールは hard-fail 既定で実装する。
- パッケージ名は DESIGN.md の正典に従い `voxbench`。rename に備え、表示名は `pyproject.toml` と `voxbench.constants.PROJECT_NAME` に集約する。
- monorepo scaffolding、JSON Schema、SQLAlchemy models、Alembic 初期 migration、registry resolver/validator、CLI、examples、acceptance tests を実装済み。
- `ruff check .` と `pytest` は通過済み。

## 今回の決定

- `meta.parent` は Phase 0 の in-memory registry では config name 参照として扱う。DB の `parent_id` は §4 どおり UUID FK として残す。
- パイプライン位置の host capability は、config stage の `host_capabilities` で宣言する保守的な解釈にした。manifest の `requires_host_capability` がその集合に含まれない場合は hard-fail。
- 隣接 stage の IO 契約は、manifest または stage override の `io.produces` と次 stage の `io.accepts` の共通キーを比較する。
- provider の `supported_codecs` が manifest にある場合は `transport.codec` と照合して hard-fail する。
- Phase 1 では Pipecat の `Pipeline([...])` 境界だけを adapter として用意し、Gemini/Asterisk の実 adapter API は未確認のため実装しない。
- StageTap artifact は local filesystem sink で保存する。MinIO は storage sink 境界の後続実装として残す。
- `POST /runs` は in-memory repository で run/recordings/spans を保持する縦切りにした。DB model/migration は同じ保存対象に合わせて追加済み。

## 未解決論点

- turn_taking 検証の hard-fail/warn の最終線引き。
- host capability を stage config の `host_capabilities` として表すか、registry 側の配置メタデータに分離するか。
- Asterisk chan_websocket と Gemini Live の Pipecat adapter API 確認後、plugin adapter を実接続に差し替える。
- MinIO SDK を使う storage sink の接続設定、bucket 作成方針、失敗時 retry 方針。
- StageTap の実 artifact 保存は Phase 1 で local sink として実装済み。ステージ別参照音声生成は Phase 2 以降の対象。
- Pipecat は Phase 1 で `Pipeline([...])` の adapter 境界のみ確認・実装済み。実 Asterisk/Gemini 接続は未実装。

## Phase 4 前段

- 最初の slice は `Run Environment Metadata + Readiness Checklist` の最小縦切りにした。
- `POST /runs` で `environment` と `readiness_checklist` を任意入力でき、未指定時は標準チェックリストを `unknown` として扱う。
- 保存・表示する値は alias/reference/status/note に限定し、URL や Slack ID 形式の raw reference は API で拒否する。
- run response、recent runs、timeline response、Web timeline header、side rail、two-run compare に環境・準備状態を追加済み。
- DB 永続化切替に備えて `runs.environment_metadata` と `runs.readiness_checklist` の JSONB migration を追加済み。

### Phase 4 次候補

- Host metrics ingestion: `cpu`, `active_tasks`, `loop_lag` を `metrics.stage = null` として取り込み、`timeline.lanes.host` に実データを流す。
- Environment-aware compare の API 化: UI 内の差分計算から、比較専用 endpoint へ移して保存済み run 以外にも展開しやすくする。
- Live Run Status preview: WebSocket 前段として `/runs` または新規 endpoint に latest host metrics と readiness/manual blocker を集約する。

## Phase 4 Host Metrics 最小 slice

- `observability.host_metrics` で宣言された `cpu`, `active_tasks`, `loop_lag` を harness で採取し、`MetricArtifact(stage=None)` として run metrics に追加した。
- `timeline.lanes.host` は stage metrics と同じ `{ts, name, value}` 形式で host metric points を返す。
- Web side rail に latest host metrics panel を追加し、CPU は `%`、loop lag は `ms` で表示する。
- 現時点の sampler は stdlib ベースの単発採取。Phase 4 live run では周期サンプリングと active run preview へ拡張する。

## Phase 4 Live Run Status preview 最小 slice

- WebSocket `/live` の前段として `GET /runs/live-preview` を追加した。
- response は recent runs を対象に、run status、environment alias、readiness summary、manual blockers、latest host metrics、violation count、tags を返す。
- Web に Live preview panel を追加し、timeline 未選択時と timeline side rail の両方で readiness/manual blockers/latest host metrics を一覧できる。
- 現時点では completed run の recent projection。次は active run lifecycle と周期 host metrics sampler をつなぐ。

## Phase 4 Active Run Lifecycle 最小 slice

- `POST /runs` は harness 実行前に `running` run を repository へ保存し、完了時に `completed`、例外時に `failed` へ更新する。
- `GET /runs/live-preview` は処理中の run も返せるようになり、`ended_at` は running 中 `null` を許容する。
- host metrics は run 開始時、各 stage tap 後、run 終了時に複数サンプルとして採取する。
- threaded API test で、遅い harness 実行中に live-preview が `running` を返し、完了後に latest host metrics を返すことを確認済み。
- 次は同期 `POST /runs` から切り離した background runner または WebSocket `/live` に進められる。

## Phase 4 Background Runner 最小 slice

- 既存 `POST /runs` は互換維持で同期実行のまま残した。
- 新規 `POST /runs/async` を追加し、`running` run を即時 `202 Accepted` で返して daemon thread で harness を実行する。
- 同期/非同期 route は config resolve、running run 作成、harness 実行、completed/failed 更新の helper を共有する。
- API test で `POST /runs/async` が即 `running` を返し、live-preview に反映され、background 完了後に `completed` と latest host metrics へ更新されることを確認済み。
- 次は WebSocket `/live` または async run 作成 UI。永続 runner に進む場合は thread ではなく job queue / DB-backed lease が必要。

## Phase 4 WebSocket Live 最小 slice

- `GET /runs/live-preview` と同じ projection を使う `WS /live` を追加した。
- 接続直後から `interval_ms` ごとに recent run status、readiness、manual blockers、latest host metrics を snapshot push する。
- Web UI は WebSocket snapshot を優先し、接続不可時は既存 REST live-preview query の結果を fallback 表示する。
- Live preview panel に WebSocket connection state badge を追加した。
- API test で `/live` が REST preview と同等の snapshot を返すことを確認済み。
- 次は async run 作成 UI、または WebSocket を差分イベント化して payload を小さくする。

## Phase 4 Async Run 作成 UI 最小 slice

- Web に `Async run` panel を追加し、JSON payload を `POST /runs/async` へ送れるようにした。
- 受理された run は Primary run に自動セットし、recent runs と live-preview を refetch する。
- payload はブラウザ内に保存せず、textarea state のみで扱う。
- エラーは panel 内に表示し、成功時は WebSocket live-preview で running/completed を追える。
- 次は example payload をサーバ側の examples から安全に生成する dev helper、または checklist/environment 専用フォーム化。

## Phase 4 Example Payload Generator 最小 slice

- `GET /runs/example-payload` を追加し、repo の `examples/configs/valid-baseline.json` と example manifests から `RunCreateRequest` 互換 payload を生成する。
- environment/readiness は alias/reference のみを含む demo/integration 用の保守的な仮値で返す。
- API test で example payload がそのまま `POST /runs/async` に使え、completed run と latest host metrics まで進むことを確認済み。
- Web の `Async run` panel に `Load example` を追加し、server-side example payload を textarea に読み込めるようにした。
- 次は environment/readiness 専用フォーム化、または loaded example の profile/server alias だけを UI で編集できる lightweight controls。

## Phase 4 Environment/Readiness Form 最小 slice

- Web の `Async run` panel に environment/readiness controls を追加した。
- `environment_profile`, `server_alias`, `integration_target_alias`, `tags` をフォームで編集すると JSON payload に反映される。
- 標準 readiness checklist の各 status を `unknown/pass/fail` select で変更でき、同じ payload に反映される。
- JSON textarea は残しており、細かい schema 調整や configs/manifests の直接編集も継続できる。
- 次は manual blockers と secret ref names の chips/edit controls、または form-only run launcher への整理。

## Phase 4 Blockers/Secret Refs Controls 最小 slice

- Web の `Async run` panel に `manual_blockers` と `secret_ref_names` の comma-separated controls を追加した。
- 入力値は environment payload に alias/reference list として反映され、secret 実値や外部 URL を扱わない方針を維持する。
- `tags`, `manual_blockers`, `secret_ref_names` の list parsing を shared helper に寄せた。
- これで demo/integration の準備状態は JSON を直接触らずに大半を調整できる。
- 次は Phase 4 前段の仕上げとして、変更内容を整理した commit/PR 用 summary、または README/DESIGN の Phase 4 操作手順追記。

## Phase 4 Docs 仕上げ slice

- README の実装済み機能リストを Phase 4 前段まで更新した。
- README に control-plane API、Web UI、example payload、async run、live-preview、`WS /live` の操作手順を追記した。
- DESIGN の run schema に `environment_metadata` と `readiness_checklist` を追記した。
- DESIGN の timeline/API surface/Phase 4 acceptance を、environment/readiness/live-preview/background run の前段と live host/SIP/RTP 後段に分けて更新した。

## Phase 4 SIP/RTP Ingest 仮 schema slice

- `POST /v1/sip-events` と `POST /v1/rtp-stats` を追加した。
- SIP は `method`, `direction`, `status_code`, `summary_alias` の structured event のみ保存し、raw SIP body/SDP は扱わない。
- RTP は `jitter_ms`, `loss_pct`, `mos` の structured quality point として保存する。
- in-memory run に紐づけ、`GET /runs/{id}/timeline` の `lanes.sip_ladder` と `lanes.rtp_quality` に相対時刻付きで出す。
- DB 永続化切替に備えて `sip_events` と `rtp_stats` の SQLAlchemy model/migration を追加した。
- API test で ingest、timeline 反映、unknown run、raw external reference rejection を確認済み。

## Phase 4 SIP/RTP Timeline UI 最小 slice

- Web side rail に Live preview / Host metrics / SIP ladder / RTP quality / Environment / Readiness の順で切り分け用パネルを並べた。
- SIP ladder は `method`, `direction`, `status_code`, `summary_alias`, `ts` の structured fields のみを表示し、raw SIP body/SDP は表示しない。
- RTP quality は latest summary、count、各 point の `jitter_ms`, `loss_pct`, `mos`, `ts` を表示する。
- two-run compare の environment deltas に SIP event count、RTP point count、latest RTP summary を最小追加した。

## Phase 4 AGC Gain Controls 最小 slice

- Web の `Async run` panel に AGC の `target_rms`, `max_gain`, `noise_floor` controls を追加した。
- controls は payload 内の `configs[].spec.media.pipeline[]` から `type = agc` の stage を探し、既存 JSON textarea と同期して params を更新する。
- packet/音声観測は引き続き structured SIP/RTP ingest と stage recording/timeline 表示まで。raw pcap/Wireshark import は未実装。
