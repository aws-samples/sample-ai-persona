# アーキテクチャ制約

## 依存方向（違反厳禁）

Router層 → Manager層 → Service層。Models は全層から参照可。逆方向禁止。

- Models (src/models/): 標準ライブラリのみインポート可。Service/Manager/Routerをインポートしてはならない
- Service層 (src/services/): Manager層・Router層をインポートしてはならない。他のServiceをインポートしてはならない（Service間のオーケストレーションはManager層が担う。service_factory.pyは例外）
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
- **例外:** テンプレートのグローバル関数として登録する表示ヘルパー（国コード→国名変換、性別コード→ラベル変換等）は、ビジネスロジックを含まない純粋なデータ参照であるため、Service層/Model層から直接インポートしてよい

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

## 例外とエラーコード

**原則:** 例外メッセージは開発者のもの、ユーザー向け文言はプレゼンテーション層のもの。

- **Models (`src/models/errors.py`):** `ErrorCode` enum と `CodedError` 基底クラス。全層から参照可
- **Service層:** 外部SDK例外を自ドメインの例外型 + `ErrorCode` に変換する。**メッセージは技術的事実のみ（英語可）**。`from e` を必ず付けてチェーンを維持する。ユーザー向け文言を持ってはならない
- **Manager層:** エラーコードを決定し、文言に必要な値は `context` に載せる（**文言そのものを組み立ててはならない**）
- **Router層:** `web/error_messages.py` の `user_message_for()` のみを参照する。**レスポンスに `str(e)` / `{e}` / `e.args` / 例外の属性を書いてはならない**

### 規約の詳細

- 新規例外は `CodedError` を継承し、コードを付与して定義する
- 1つの例外型が複数のユーザー向け状況を表す場合（`FileUploadError` 等）は、例外クラスを増やさず `raise` 時に `code=` を指定する
- 文言カタログは `web/error_messages.py` に集約する。`_CATALOG` を直接参照してはならない（i18n拡張時の変更を1ファイルに閉じるため）
- `context` に載せてよいのは**ユーザーに見せて安全な値**（サイズ上限、件数上限、対応形式一覧等）のみ。ID・ファイルパス・SDK例外文はログにのみ出す
- 内部エラーの詳細は `logger.*(..., exc_info=True)` でログに出す。`traceback.format_exc()` は使わない
- フィールド単位のバリデーションは、フィールドごとにコードを作らず「バリデーション種別 + `context["field"]` の安定キー」で表現する（キー→表示名の写像はカタログが持つ）

### リクエスト検証エラー（422）

`Form(...)` / Pydanticボディの検証失敗は FastAPI が Router に入る**前**に検出するため、Router内の `except` では捕捉できずカタログを経由しない。`web/main.py` のグローバルハンドラ（`RequestValidationError`）で処理する。

- htmx リクエスト（`HX-Request` ヘッダー）にはパーシャルHTMLを返し、それ以外はFastAPI標準のJSON応答を維持する（`web/routers/api.py` のJSONクライアント互換のため）
- `exc.errors()` の `input` には利用者の入力値が載る。**レスポンスに転写してはならない**（Router層で `str(e)` を書かない原則と同じ扱い）
- Router 個別に `try/except` を足して対処しない（`Form(...)` は40箇所以上あり、分散させるとこの穴が再発する）

### 検査

- `tests/api/test_error_exposure.py` が `web/routers/` をASTで走査し、例外変数がログ・`user_message_for()` 以外から参照されていないことを機械的に検査する
- 同ファイルの `TestRequestValidationErrorHandling` が422のグローバルハンドラの登録と挙動（文言・入力値の非転写・JSON経路の維持）を検査する
- `tests/unit/test_error_messages.py` が全 `ErrorCode` のカタログ登録漏れを検知する
- テストで文言をアサートしない。`tests/error_helpers.raises_code()` でエラーコードを検証する

詳細な設計背景は `docs/note/exception-message-design.md` を参照。

## テスト

- マーカー: `unit`(src/managers), `integration`(src/services), `api`(web/routers)
- 外部サービスモック: DynamoDB/S3は`moto`、AI系は`unittest.mock.Mock`
- Manager層テスト: コンストラクタDIでモック注入
- Router層テスト: `reset_singletons` autouseフィクスチャでテスト間分離
