# 進捗

## Phase 0

- §2 と §15 を確認し、Phase 0 のみに限定して着手。
- 具体値は `examples/` のサンプルデータに限定する方針。
- turn_taking の hard-fail/warn 境界は付録で未確定のため、Phase 0 acceptance の対象ルールは hard-fail 既定で実装する。
- パッケージ名は DESIGN.md の正典に従い `voxbench`。rename に備え、表示名は `pyproject.toml` と `voxbench.constants.PROJECT_NAME` に集約する。

## 未解決論点

- turn_taking 検証の hard-fail/warn の最終線引き。
- StageTap やステージ別参照音声生成は Phase 2 以降の対象であり、Phase 0 では未実装。
- PipeCat API は Phase 1 以降の対象であり、Phase 0 では呼び出しもラップ実装も行わない。

