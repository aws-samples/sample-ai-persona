# Tasks: D360 連携ペルソナ生成

## Implementation Checklist

### Phase 1: 基盤
- [ ] `src/config.py` に D360 設定を追加（`D360_RUNTIME_ARN`, `D360_REGION`, `ENABLE_D360_INTEGRATION`）
- [ ] `src/services/d360_service.py` を新規作成（AgentCore Runtime 呼び出し、SSE パース）

### Phase 2: ペルソナ生成ロジック
- [ ] `src/managers/persona_manager.py` に `generate_personas_from_dwh()` を追加
- [ ] D360 質問生成プロンプトを作成（analysis_angle → D360 への質問）
- [ ] D360 分析結果 → ペルソナ生成プロンプトを作成（data_type="dwh" 対応）

### Phase 3: Web UI
- [ ] `web/routers/persona.py` で `data_type="dwh"` のハンドリング追加
- [ ] `web/templates/persona/generation.html` に「DWH（D360連携）」オプション追加
- [ ] data_type 切り替え時の UI 動的表示（ファイルアップロード ↔ 分析の切り口入力）

### Phase 4: 設定画面
- [ ] `web/routers/settings.py` に D360 接続設定エンドポイント追加
- [ ] `web/templates/settings/` に D360 設定セクション UI 追加

### Phase 5: エラーハンドリング・仕上げ
- [ ] D360 未設定時のバリデーションとエラーメッセージ
- [ ] D360 応答エラー・タイムアウト時の SSE エラー通知
- [ ] 動作確認（D360 接続 → 分析 → ペルソナ生成の E2E フロー）

## Validation
- [ ] D360 未設定時に適切なエラーメッセージが表示される
- [ ] 分析の切り口入力 → D360 問い合わせ → ペルソナ生成が一連で動作する
- [ ] SSE で進捗表示（問い合わせ中 → 生成中 → 完了）が正しく動作する
- [ ] 既存のファイルアップロードによるペルソナ生成が影響を受けない

---
**Created**: 2026-04-22
