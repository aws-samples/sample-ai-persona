# Tasks: Agentモード議論のコンテキスト管理改善

## Implementation Checklist

### Step 1: 履歴クリアメソッド追加 + テスト
- [x] `PersonaAgent.clear_conversation_history()` 実装
- [x] `FacilitatorAgent.clear_conversation_history()` 実装
- [x] 両メソッドのユニットテスト作成
- [x] 既存テスト通過確認

### Step 2: ファシリテータのプロンプト改善 + テスト
- [x] `FacilitatorAgent.create_prompt_for_persona()` に `round_summaries` パラメータ追加
- [x] 要約コンテキスト + 直近3件の生発言を含むプロンプト生成ロジック実装
- [x] `FacilitatorAgent.summarize_round()` の要約プロンプト改善
- [x] ユニットテスト作成（要約あり/なし、直近発言数のバリエーション）
- [x] 既存テスト通過確認

### Step 3: PersonaAgent の二重コンテキスト除去 + テスト
- [x] `PersonaAgent._build_prompt_with_context()` を簡素化（promptをそのまま返す）
- [x] ユニットテスト更新
- [x] 既存テスト通過確認

### Step 4: AgentDiscussionManager ラウンドループ変更 + テスト
- [x] `start_agent_discussion()` に `round_summaries` 蓄積 + ラウンド間リセット追加
- [x] `start_agent_discussion_streaming()` に同様の変更
- [x] インテグレーションテスト作成（ラウンド間の履歴リセット確認、要約蓄積確認）
- [x] 既存テスト通過確認

## Validation
- [x] `uv run pytest -m unit` 全パス (625 passed)
- [x] `uv run pytest -m integration` 全パス (98 passed, 2 skipped)
- [x] 各ステップでコミット

---
**Created**: 2026-03-30
