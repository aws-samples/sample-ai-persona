# Tasks: データ＋プロンプトによるペルソナ作成の柔軟性強化

## Implementation Checklist

### Backend
- [x] `AgentService.create_persona_generation_agent(data_type, data_description, custom_prompt, persona_count, use_mcp)` 実装
- [x] `PersonaManager.generate_personas(file_contents, data_type, data_description, custom_prompt, persona_count)` 実装
- [x] `POST /persona/upload` エンドポイント拡張（複数ファイル対応）
- [x] `POST /persona/generate` エンドポイント置き換え（統一ペルソナ生成）
- [x] 旧エンドポイント（`/persona/generate-multiple`）の廃止

### Frontend
- [x] `web/templates/persona/generation.html` 統一UIに書き換え
- [x] `web/templates/persona/partials/uploaded_files.html` 新規作成 → 不要（統一フォームで直接ファイル選択）
- [x] ファイルアップロード → データ種別選択 → プロンプト → 生成のフロー実装

## Validation
- [x] インタビューデータ（プリセット）で1ペルソナ生成が動作する
- [x] 市場調査レポート（プリセット）で複数ペルソナ生成が動作する
- [ ] レビューデータ（プリセット）でペルソナ生成が動作する
- [x] 「その他」＋フリーテキスト説明でペルソナ生成が動作する
- [x] カスタムプロンプト付きでペルソナ生成が動作する
- [ ] 複数ファイルアップロードが動作する
- [ ] 生成結果の選択・保存が動作する

---
**Created**: 2026-04-05
