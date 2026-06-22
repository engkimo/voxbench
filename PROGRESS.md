# 進捗

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

## 未解決論点

- turn_taking 検証の hard-fail/warn の最終線引き。
- host capability を stage config の `host_capabilities` として表すか、registry 側の配置メタデータに分離するか。
- StageTap やステージ別参照音声生成は Phase 2 以降の対象であり、Phase 0 では未実装。
- PipeCat API は Phase 1 以降の対象であり、Phase 0 では呼び出しもラップ実装も行わない。
