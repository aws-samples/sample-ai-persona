# アーキテクチャ制約

## 依存方向（違反厳禁）

Router層 → Manager層 → Service層。Models は全層から参照可。逆方向禁止。

- Models (src/models/): 標準ライブラリのみインポート可。Service/Manager/Routerをインポートしてはならない
- Service層 (src/services/): Manager層・Router層をインポートしてはならない
- Manager層 (src/managers/): Router層をインポートしてはならない。他のManagerをインポートしてはならない
- Shared (src/managers/shared/): Manager層の共有ユーティリティ。Service層・Router層からのインポート禁止。他のManagerからインポート可
- Router層 (web/routers/): Service層を直接使ってはならない（Manager経由で操作する）

## 各層の責務と関心の分離

### Models (src/models/)
**責務:** データ構造の定義と変換のみ

- selfを変更するメソッドを定義してはならない（更新は新インスタンスを返す）
- `to_dict()`でNone値のフィールドを含めてはならない
- 既存モデルの`create_new()`, `update()`, `to_dict()`, `from_dict()`パターンに従うこと
- 標準ライブラリのみインポート可。外部依存・他層への依存禁止

### Router層 (web/routers/)
**責務:** HTTPリクエスト/レスポンスの変換、非同期化制御のみ

- ビジネスロジックを書いてはならない（Manager層に委譲）
- Service層を直接インポート・使用してはならない
- Managerはモジュールレベル変数 + `get_*_manager()`遅延初期化で保持すること
- 同期的なAI/DB処理は`ThreadPoolExecutor`で非同期化すること
- エラーハンドリングはManager層の例外をHTTPレスポンスに変換するだけ

### Manager層 (src/managers/)
**責務:** ビジネスロジック、ワークフロー制御、バリデーション、例外変換

- Manager固有の例外クラスを定義し、Service層の例外をキャッチして変換すること
- コンストラクタでServiceをオプション引数として受け取り、未指定時は`service_factory`から取得すること
- **ビジネスロジックの具体例:**
  - 入力バリデーション（ペルソナ数制限、トピック長制限、必須フィールド検証）
  - ワークフロー制御（議論ラウンド管理、発言者選択、フェーズ別プロンプト構築）
  - 状態遷移判定（ステータス変更可否、continue/stop判定）
  - データ集約・変換（複数Serviceの結果を組み合わせた応答構築）
- HTTP通信・ファイルI/O・データフレーム操作・boto3直接呼び出しを書いてはならない（Service層に委譲）
- 他のManagerをインポートしてはならない（共有ロジックは`shared/`に配置）

### Shared (src/managers/shared/)
**責務:** 複数のManagerが共通で使うユーティリティ関数

- ビジネスルール判定を含まない純粋なヘルパー（ドキュメント読み込み、ContentBlock構築等）
- Service層・Router層からのインポート禁止
- 他のManagerからインポート可

### Service層 (src/services/)
**責務:** 外部システムとの通信、SDK呼び出し、リトライ制御のみ

- 環境変数は`src/config.py`経由で参照すること。直接`os.environ`を使ってはならない
- Service固有の例外クラスを定義すること
- リトライ・タイムアウト・バックオフ処理はこの層に閉じること
- **Service層に書いてはならないものの具体例:**
  - ビジネスルール判定（件数上下限、ステータス遷移、フェーズ別分岐）
  - ワークフロー制御（ラウンド管理、発言順序決定、continue判定）
  - プロンプト構築のうちビジネスロジックに依存する部分（フェーズ別指示、コンテキスト取捨選択）
  - 入力バリデーション（Manager層で実施済みのものを重複して検証しない）
- **Service層に残すべきものの具体例:**
  - API呼び出し（Bedrock converse/invoke、DynamoDB CRUD、S3操作）
  - SDK固有のデータ変換（APIレスポンスのパース、リクエストフォーマット構築）
  - エージェントインスタンス生成・破棄（Strands Agent SDK操作）
  - クエリ実行（DuckDB、Parquet）

## テスト

- マーカー: `unit`(src/managers), `integration`(src/services), `api`(web/routers)
- 外部サービスモック: DynamoDB/S3は`moto`、AI系は`unittest.mock.Mock`
- Manager層テスト: コンストラクタDIでモック注入
- Router層テスト: `reset_singletons` autouseフィクスチャでテスト間分離
