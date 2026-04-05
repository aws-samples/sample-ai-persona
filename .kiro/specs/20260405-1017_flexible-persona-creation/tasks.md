# Tasks: データ＋プロンプトによるペルソナ作成の柔軟性強化

## Implementation Checklist

### Backend
- [ ] `AgentService.generate_personas_flexible(data_text, data_type, data_description, custom_prompt, persona_count)` 実装
- [ ] `PersonaManager.generate_personas_flexible(file_contents, data_type, data_description, custom_prompt, persona_count)` 実装
- [ ] `POST /persona/upload-files` エンドポイント実装（複数ファイルアップロード）
- [ ] `POST /persona/generate-flexible` エンドポイント実装（統一ペルソナ生成）
- [ ] 旧エンドポイント（`/persona/upload`, `/persona/generate`, `/persona/generate-multiple`）の廃止

### Frontend
- [ ] `web/templates/persona/generation.html` 統一UIに書き換え
- [ ] `web/templates/persona/partials/uploaded_files.html` 新規作成
- [ ] ファイルアップロード → データ種別選択 → プロンプト → 生成のフロー実装

## Validation
- [ ] インタビューデータ（プリセット）で1ペルソナ生成が動作する
- [ ] 市場調査レポート（プリセット）で複数ペルソナ生成が動作する
- [ ] レビューデータ（プリセット）でペルソナ生成が動作する
- [ ] 「その他」＋フリーテキスト説明でペルソナ生成が動作する
- [ ] カスタムプロンプト付きでペルソナ生成が動作する
- [ ] 複数ファイルアップロードが動作する
- [ ] 生成結果の選択・保存が動作する

---
**Created**: 2026-04-05
