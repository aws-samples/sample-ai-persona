# Design: Agentモード議論のコンテキスト管理改善

## Components

### 変更対象ファイル
```
src/services/agent_service.py
├── PersonaAgent.clear_conversation_history()    — 新規
├── PersonaAgent._build_prompt_with_context()    — 変更
├── PersonaAgent.respond()                       — シグネチャ変更なし
├── FacilitatorAgent.clear_conversation_history() — 新規
├── FacilitatorAgent.create_prompt_for_persona()  — シグネチャ変更
└── FacilitatorAgent.summarize_round()            — プロンプト改善

src/managers/agent_discussion_manager.py
├── start_agent_discussion()           — ラウンドループ変更
└── start_agent_discussion_streaming() — ラウンドループ変更
```

## API Design

### PersonaAgent.clear_conversation_history()
```python
def clear_conversation_history(self) -> None:
    """Strands Agent内部の会話履歴をクリア（システムプロンプトは保持）"""
```

### FacilitatorAgent.clear_conversation_history()
```python
def clear_conversation_history(self) -> None:
    """Strands Agent内部の会話履歴をクリア"""
```

### FacilitatorAgent.create_prompt_for_persona() — シグネチャ変更
```python
def create_prompt_for_persona(
    self,
    persona_agent: PersonaAgent,
    topic: str,
    recent_messages: List[Message],      # 変更: all_messages[-3:]
    round_summaries: List[str] | None = None,  # 追加
) -> str:
```

## Implementation Strategy

### データフロー（ラウンドループ）
```
round_summaries: list[str] = []  # 議論全体で蓄積

while facilitator.should_continue():
    ラウンド開始:
        if current_round > 1:
            全ペルソナ.clear_conversation_history()
            ファシリテータ.clear_conversation_history()

    各ペルソナ発言:
        prompt = facilitator.create_prompt_for_persona(
            speaker, topic,
            recent_messages=all_messages[-3:],
            round_summaries=round_summaries,
        )
        statement = speaker.respond(prompt, context=None)

    ラウンド終了:
        summary = facilitator.summarize_round(...)
        round_summaries.append(summary)
```

### PersonaAgent._build_prompt_with_context() の変更
- context引数を使わず、promptをそのまま返す
- コンテキストは `create_prompt_for_persona()` で構築済みのため二重付加を防止

### FacilitatorAgent.summarize_round() の要約プロンプト改善
- 各参加者の主要な意見や立場
- 参加者間の共通点や対立点
- 新たに出た視点や気づき
- 次のラウンドで深掘りすべき論点
- 3-5文で要約

### トークン数の変化（5ペルソナ × 5ラウンド）
| | 現状 | 変更後 |
|---|---|---|
| Strands内部履歴（ラウンド5時点） | ~20回分 15K-25K+ | 0（クリア済み） |
| プロンプト内コンテキスト | 直近3-5件 ~1K | 要約4件 + 直近3件 ~2-3K |
| 合計 | 15K-25K+ | 2-3K |

---
**Created**: 2026-03-30
