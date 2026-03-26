# AIペルソナシステム トラブルシューティングガイド

## 一般的な問題と解決方法

### システム起動時の問題

#### 問題: アプリケーションが起動しない

**症状:**
```
ModuleNotFoundError: No module named 'fastapi'
```

**解決方法:**
1. 依存関係を再インストール
```bash
uv sync
```

2. FastAPIが正しくインストールされているか確認
```bash
uv run python -c "import fastapi; print(fastapi.__version__)"
```

#### 問題: ポート8000が使用中

**症状:**
```
ERROR: [Errno 48] Address already in use
```

**解決方法:**
1. 別のポートで起動
```bash
uv run uvicorn web.main:app --reload --port 8001
```

2. 使用中のプロセスを確認して終了
```bash
lsof -i :8000
kill -9 <PID>
```

#### 問題: uvが見つからない

**症状:**
```
Command 'uv' not found
```

**解決方法:**
1. uvをインストール
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

2. パスが通っているか確認
```bash
uv --version
```

#### 問題: Python 3.13が見つからない

**症状:**
```
Python 3.13 not found
```

**解決方法:**
```bash
uv python install 3.13
uv python pin 3.13
```

#### 問題: 依存関係のインストールエラー

**症状:**
```
Failed to resolve dependencies
```

**解決方法:**
```bash
uv cache clean
uv sync --reinstall
```

#### 問題: Tailwind CSSのスタイルが適用されない

**解決方法:**
```bash
# CSSをビルド
./scripts/build-css.sh --minify

# 静的ファイルが正しく配置されているか確認
ls -la web/static/css/
```

ブラウザのキャッシュもクリア（Cmd+Shift+R）してください。

---

### AWS認証・Bedrock接続の問題

#### 問題: AWS認証エラー

**症状:**
- ペルソナ生成時に「認証エラー」が表示される
- 議論開始時にエラーが発生する

**解決方法:**
1. 認証情報の確認
```bash
aws sts get-caller-identity
```

2. 環境変数の確認
```bash
echo $AWS_REGION
echo $AWS_ACCESS_KEY_ID
```

3. IAM権限の確認
   - 必要な権限: `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`
   - バッチ推論使用時: `bedrock:CreateModelInvocationJob`, `bedrock:GetModelInvocationJob`

#### 問題: Bedrock APIレート制限

**症状:**
```
Rate limit exceeded
```

**解決方法:**
1. 少し時間をおいて再試行
2. 同時実行数を減らす
3. AWSサポートに制限緩和を依頼

---

### DynamoDBの問題

#### 問題: DynamoDBテーブルが見つからない

**症状:**
```
DatabaseError: Required DynamoDB tables not found
```

**解決方法:**
1. CDKでバックエンドリソースがデプロイ済みか確認
```bash
cd cdk && npx cdk diff
```

2. AWS認証情報とリージョンを確認
```bash
aws sts get-caller-identity
echo $DYNAMODB_REGION
echo $DYNAMODB_TABLE_PREFIX
```

3. テーブルの存在確認
```bash
aws dynamodb list-tables --region $DYNAMODB_REGION | grep AIPersona
```

#### 問題: データベース接続エラー

**症状:**
- 「データベースに接続できません」エラー
- ペルソナ保存時にエラー

**解決方法:**
1. 健全性チェック
```bash
uv run python -c "from src.services.service_factory import ServiceFactory; db = ServiceFactory().get_database_service(); print('健全性:', db.check_database_health())"
```

2. 環境変数の確認
```bash
# .envファイルの設定を確認
cat .env | grep DYNAMODB
```

#### 問題: データベース検索が遅い

**解決方法:**
1. DynamoDBのキャパシティモードを確認
```bash
aws dynamodb describe-table --table-name AIPersona_Discussions --region $DYNAMODB_REGION
```

2. 必要に応じてオンデマンドモードへの切り替えを検討

#### 問題: データベース情報の確認

```bash
uv run python -c "
from src.services.service_factory import ServiceFactory
import json
db = ServiceFactory().get_database_service()
print(json.dumps(db.get_database_info(), indent=2, ensure_ascii=False))
"
```

---

### インタビューモード関連

#### 問題: インタビューセッション作成エラー

**症状:**
- 「インタビューセッションの作成に失敗しました」エラー

**対処法:**
1. 最低1体のペルソナが選択されているか確認
2. AWS認証情報とBedrock権限を確認
3. アプリケーションを再起動
```bash
uv run python run_htmx.py
```

#### 問題: チャットの応答が返らない

**症状:**
- メッセージ送信後にローディングが終わらない
- 「ペルソナが回答を考えています...」が継続表示される

**対処法:**
1. ブラウザの開発者ツール（F12）でConsole/Networkタブを確認
2. ページをリロード（Cmd+Shift+R）
3. Amazon Bedrockのサービス状態を確認
4. 問題が続く場合は新しいセッションを作成

#### 問題: セッション保存エラー

**対処法:**
1. セッション名が空でないか確認
2. メッセージが含まれているか確認
3. DynamoDBへの接続を確認

---

### ファイルアップロードの問題

#### 問題: ペルソナ生成用ファイルのアップロードが失敗する

**対処法:**
1. ファイル形式の確認
   - 対応形式: `.txt`, `.md`
   - サイズ上限: 10MB
   - エンコーディング: UTF-8、Shift_JIS、EUC-JP
   - 最低50文字以上の内容が必要

2. ストレージの確認
```bash
# S3使用時
echo $S3_BUCKET_NAME
aws s3 ls s3://$S3_BUCKET_NAME/uploads/

# ローカル使用時（S3_BUCKET_NAME未設定）
ls -la uploads/
```

#### 問題: アップロードディレクトリが存在しない（ローカル使用時）

**症状:**
```
FileNotFoundError: uploads directory not found
```

**解決方法:**
```bash
mkdir -p uploads
chmod 755 uploads
```

#### 問題: S3へのアップロードが失敗する

**対処法:**
1. AWS認証情報の確認
```bash
aws sts get-caller-identity
```

2. S3バケットの存在と権限確認
```bash
aws s3 ls s3://$S3_BUCKET_NAME/
```

3. 必要なIAM権限: `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`

4. `AWS_REGION`がS3バケットのリージョンと一致しているか確認

---

### マルチモーダルドキュメントの問題

#### 問題: 議論用ドキュメントのアップロードが失敗する

**対処法:**
1. ファイル形式の確認
   - 対応形式: PNG, JPEG, PDF のみ
   - 個別ファイル: 最大10MB
   - 合計: 最大32MB（Bedrock API制限）

2. ファイルの整合性確認
```bash
file --mime-type your_file.png
```

#### 問題: PDFドキュメントが正しく処理されない

**対処法:**
1. PDFが破損していないか確認
2. ページ数を確認（最大100ページ）
3. パスワード保護されていないか確認
4. テキストを含んでいるか確認（画像のみのPDFは非対応の場合あり）

---

### マスアンケート機能の問題

#### 問題: アンケート実行がエラーになる

**症状:**
- ステータスが「エラー」になる
- 「バッチ推論の実行に失敗しました」エラー

**対処法:**
1. IAMロールの確認
```bash
echo $BEDROCK_BATCH_ROLE_ARN
```
   - 未設定の場合はロールを作成:
   ```bash
   uv run python scripts/create_bedrock_batch_role.py --bucket-name $S3_BUCKET_NAME
   ```

2. S3バケットの確認
```bash
echo $S3_BUCKET_NAME
aws s3 ls s3://$S3_BUCKET_NAME/
```

3. ペルソナ数の確認
   - Bedrock Batch Inferenceは最低100件の入力が必要
   - フィルタ条件が厳しすぎないか確認

4. モデルアクセスの確認
   - Claude 4.5 Haikuモデルへのアクセス権限があるか確認

#### 問題: ペルソナデータセットの読み込みが遅い

**対処法:**
1. 初回はHugging FaceからダウンロードしParquet形式でS3に配置するため30-120秒かかります
2. 2回目以降はS3上のParquetに直接クエリするため高速です
3. S3接続・AWS認証情報が有効か確認
4. DuckDB/Polarsがインストールされているか確認
```bash
uv run python -c "import duckdb; import polars; print('OK')"
```

#### 問題: カスタムCSVのアップロードが失敗する

**対処法:**
1. CSVファイル形式の確認
   - UTF-8エンコーディング
   - ヘッダー行が存在すること
   - カンマ区切り
2. ファイルサイズ: 最大100MB
3. 特殊文字やエスケープの確認
```bash
head -5 your_file.csv
wc -l your_file.csv
```

#### 問題: CSVダウンロードが失敗する

**対処法:**
1. 署名付きURLの有効期限は5分です。期限切れの場合はページをリロード
2. S3バケットへのGetObject権限を確認

#### 問題: インサイトレポートの生成に失敗する

**対処法:**
1. Amazon Bedrockのサービス状態を確認
2. API制限に達していないか確認
3. 時間をおいて再試行

---

### 長期記憶（AgentCore Memory）の問題

#### 問題: 長期記憶が動作しない

**対処法:**
1. 環境変数の確認
```bash
echo $ENABLE_LONG_TERM_MEMORY    # true であること
echo $AGENTCORE_MEMORY_ID
echo $AGENTCORE_MEMORY_REGION
echo $SUMMARY_MEMORY_STRATEGY_ID
echo $SEMANTIC_MEMORY_STRATEGY_ID
```

2. AgentCore Memoryリソースが作成済みか確認
3. IAMロールにAgentCore Memoryへのアクセス権限があるか確認

---

### 外部データセット連携の問題（実験的機能）

#### 問題: MCPサーバーが起動しない

**対処法:**
1. uvxがインストールされているか確認
```bash
uvx --version
```

2. MotherDuck MCPサーバーの確認
```bash
uvx mcp-server-motherduck --help
```

3. 設定画面でデータセット連携が有効になっているか確認

#### 問題: データセットアップロードが失敗する

**対処法:**
1. CSVファイル形式: UTF-8、ヘッダー行あり、カンマ区切り
2. ファイルサイズ: 最大100MB
3. エンコーディングの変換
```bash
file -i your_file.csv
iconv -f SHIFT_JIS -t UTF-8 your_file.csv > your_file_utf8.csv
```

#### 問題: ペルソナがデータにアクセスできない

**対処法:**
1. ペルソナ詳細画面で紐付け設定を確認
2. 紐付けキー（user_id等）が正しいか確認
3. MCPサーバーが「起動中」か確認（設定ページ）
4. 不安定な場合はリトライ

---

### UI表示の問題

#### 問題: ページが更新されない

**症状:**
- ボタンをクリックしても反応がない
- フォーム送信後に画面が更新されない

**解決方法:**
1. ブラウザの開発者ツール（F12）でエラーを確認
2. htmxが読み込まれているか確認
```javascript
console.log(htmx.version);
```
3. ブラウザのキャッシュをクリア（Cmd+Shift+R）

#### 問題: リアルタイム表示（SSE）が動作しない

**症状:**
- 議論やインタビューのストリーミングが表示されない
- 接続エラーが表示される

**解決方法:**
1. SSEサポートの確認
```javascript
console.log('SSE supported:', typeof EventSource !== 'undefined');
```

2. ネットワークタブでSSE接続を確認
   - レスポンスタイプが `text/event-stream` であること

3. プロキシやファイアウォールがSSE接続をブロックしていないか確認

4. デバッグログで確認
```bash
uv run uvicorn web.main:app --reload --log-level debug
```

#### 問題: ストリーミング中に接続が切断される

**対処法:**
1. ネットワーク接続の安定性を確認
2. プロキシ使用時はタイムアウト設定を確認
```nginx
# nginx設定例
proxy_read_timeout 300s;
proxy_buffering off;
```
3. ページをリロードして再試行

---

## エラーメッセージ別対処法

### ファイル関連

| エラーメッセージ | 原因 | 対処法 |
|---|---|---|
| ファイル形式が正しくありません | 非対応ファイル形式 | .txt, .md を使用 |
| ファイルサイズが大きすぎます | 10MB超過 | ファイルサイズを削減 |
| 許可されていないファイル形式です | ドキュメント形式エラー | PNG, JPEG, PDF を使用 |
| ファイルサイズが制限を超えています | 個別10MB/合計32MB超過 | ファイルサイズを削減 |

### AI生成関連

| エラーメッセージ | 原因 | 対処法 |
|---|---|---|
| ペルソナの生成に失敗しました | API接続エラー | AWS認証情報確認 |
| 議論の実行に失敗しました | レート制限/APIエラー | 時間をおいて再試行 |
| インサイトの生成に失敗しました | データ不足/APIエラー | 議論内容を確認 |

### マスアンケート関連

| エラーメッセージ | 原因 | 対処法 |
|---|---|---|
| テンプレート名は空白のみでは登録できません | テンプレート名が空白 | 有効な名前を入力 |
| 質問が1つも含まれていません | 質問未追加 | 質問を1つ以上追加 |
| 選択式質問には2つ以上の選択肢が必要です | 選択肢不足 | 選択肢を2つ以上追加 |
| 対象ペルソナ数は100〜10000の範囲で指定してください | ペルソナ数範囲外 | 100〜10000の範囲で入力 |
| BEDROCK_BATCH_ROLE_ARN が設定されていません | IAMロール未設定 | 環境変数にロールARNを設定 |
| バッチ推論の実行に失敗しました | バッチ推論エラー | IAMロール・S3権限確認 |
| ペルソナデータセットの準備に失敗しました | S3/DuckDB接続エラー | S3権限・Parquetファイル確認 |
| 画像は1枚まで添付できます | テンプレート画像数超過 | 画像を1枚に減らす |

### データセット連携関連

| エラーメッセージ | 原因 | 対処法 |
|---|---|---|
| MCPサーバーの起動に失敗しました | uvx未インストール/権限不足 | uvxインストール確認 |
| CSVファイルの解析に失敗しました | 不正なCSV形式 | UTF-8エンコーディング確認 |
| データセットが見つかりません | 削除済み/ID不正 | データセット一覧を確認 |
| SQLクエリの実行に失敗しました | クエリエラー/認証問題 | リトライ、MCPサーバー再起動 |

### データベース関連

| エラーメッセージ | 原因 | 対処法 |
|---|---|---|
| データの保存に失敗しました | DB接続エラー | DynamoDB接続確認 |
| データの取得に失敗しました | 権限不足 | IAM権限確認 |
| Required DynamoDB tables not found | テーブル未作成 | CDKデプロイを確認 |

---

## ログの確認方法

### アプリケーションログ

```bash
# デバッグモードで起動
uv run uvicorn web.main:app --reload --log-level debug
```

### エラーログの確認

- ブラウザ: 開発者ツール（F12）→ Console / Network タブ
- サーバー: ターミナルの標準出力
- ECS環境: CloudWatch Logs

---

## 緊急時の対処

### システムが完全に動作しない場合

```bash
# 1. 仮想環境の削除と再構築
rm -rf .venv
uv cache clean
uv sync

# 2. CSSの再ビルド
./scripts/build-css.sh --minify

# 3. 環境変数の再設定
cp .env.example .env
# .env を編集してAWSリソース名等を設定

# 4. アプリケーション起動
uv run python run_htmx.py
```

---
