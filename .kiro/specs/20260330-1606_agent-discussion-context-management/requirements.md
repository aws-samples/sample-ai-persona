# Requirements: Agentモード議論のコンテキスト管理改善

## Background & Context
### User Problems
- Agentモード議論でラウンドが進むにつれ、Strands Agent内部の会話履歴（`agent.messages`）が無制限に蓄積される
- 5ペルソナ×5ラウンドで30回のAPI呼び出し、後半ラウンドでは15K-25K+トークンのコンテキストが送信される
- レイテンシ増大、コスト増加、接続エラー（タイムアウト）が報告されている

### Related Issues
- `PersonaAgent._build_prompt_with_context()` と `FacilitatorAgent.create_prompt_for_persona()` で二重にコンテキストが付加されている
- ファシリテータの `summarize_round()` も同じエージェントを使い回し、会話履歴が膨張

## Objectives
- Strands Agent内部の会話履歴をラウンドごとにリセットし、トークン消費を抑制
- ファシリテータの要約を「圧縮コンテキスト」として蓄積し、議論の深まりを保持
- 直近3件の生発言（ファシリテータ含む）で会話の自然な接続を維持

## Scope
### In Scope
- `AgentDiscussionManager.start_agent_discussion()` のコンテキスト管理
- `AgentDiscussionManager.start_agent_discussion_streaming()` のコンテキスト管理
- `PersonaAgent` / `FacilitatorAgent` の履歴クリアメソッド追加
- `FacilitatorAgent.create_prompt_for_persona()` の要約コンテキスト対応
- `PersonaAgent._build_prompt_with_context()` の二重コンテキスト除去
- `FacilitatorAgent.summarize_round()` の要約プロンプト改善

### Out of Scope
- Classicモード（ワンショットのため問題小）
- Interviewモード（別途対応）
- メタ要約（要約の要約）— 最大10ラウンドの規模では不要と判断
- 構造化コンテキスト（アプローチ2）— 将来の拡張として検討

## Detailed Requirements

### コンテキスト管理方針
- 全ラウンドの要約を持ち回す（メタ圧縮なし、重要コンテキストの抜け落ち防止）
- 直近3件の生発言はラウンドをまたいで `all_messages[-3:]` から取得（フィルタなし）
- ファシリテータの要約も直近発言に含める（次ラウンドへの橋渡し・論点整理として機能）
- Strands Agent内部履歴は `agent.messages.clear()` でリセット（選択肢A）

### プロンプト構成（ラウンド2以降）
```
議論テーマ「{topic}」について意見を述べてください。

## これまでの議論の要約
ラウンド1: {round_summaries[0]}
ラウンド2: {round_summaries[1]}

## 直近の発言
- {all_messages[-3].persona_name}: {content}
- {all_messages[-2].persona_name}: {content}
- {all_messages[-1].persona_name}: {content}

他の参加者の意見に対する反応や、自分自身の価値観に基づいた視点を提供してください。
```

---
**Created**: 2026-03-30
