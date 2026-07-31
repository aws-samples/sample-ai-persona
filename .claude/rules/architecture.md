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

- **Models (`src/models/errors.py`):** `ErrorCode` enum（`ErrorKind` 付き）と `CodedError` 基底クラス。全層から参照可
- **Service層:** 外部SDK例外を自ドメインの例外型 + `ErrorCode` に変換する。**メッセージは技術的事実のみ（英語可）**。`from e` を必ず付けてチェーンを維持する。ユーザー向け文言を持ってはならない
- **Manager層:** エラーコードを決定し、文言に必要な値は `context` に載せる（**文言そのものを組み立ててはならない**）
- **Router層:** `web/error_messages.py` の `user_message_for()` のみを参照する。**レスポンスに `str(e)` / `{e}` / `e.args` / 例外の属性を書いてはならない**

### 規約の詳細

- 新規例外は `CodedError` を継承し、コードを付与して定義する
- **新規 `ErrorCode` は `(値, ErrorKind)` のタプルで定義する**（分類を省略すると import 時に `TypeError` になる。分類漏れを構造で防ぐため）
- 1つの例外型が複数のユーザー向け状況を表す場合（`FileUploadError` 等）は、例外クラスを増やさず `raise` 時に `code=` を指定する
- 文言カタログは `web/error_messages.py` に集約する。`_CATALOG` を直接参照してはならない（i18n拡張時の変更を1ファイルに閉じるため）
- `context` に載せてよいのは**ユーザーに見せて安全な値**（サイズ上限、件数上限、対応形式一覧等）のみ。ID・ファイルパス・SDK例外文はログにのみ出す
- 内部エラーの詳細は `logger.*(..., exc_info=True)` でログに出す。`traceback.format_exc()` は使わない
- フィールド単位のバリデーションは、フィールドごとにコードを作らず「バリデーション種別 + `context["field"]` の安定キー」で表現する（キー→表示名の写像はカタログが持つ）

### エラーの分類（`ErrorKind`）

`ErrorCode` は「何が起きたか」に加えて `kind`（ユーザーが次に何をすればよいか）を持つ。表示層はHTTPステータスや個別コードではなく **`kind` で分岐する**（Issue #117）。

| `ErrorKind` | 意味 | 想定する表示 |
|---|---|---|
| `VALIDATION` | 入力を直せば解決 | フォーム内にインライン表示。**入力を保持する** |
| `CAPACITY` | 量を減らせば解決 | フォーム近傍。上限値を明示（`context` で補間） |
| `NOT_FOUND` | 対象が無い / 未生成 | 該当領域を置換し、復帰リンクを出す |
| `CONFIG` | 運用者の設定が必要 | 設定画面へ誘導 |
| `TRANSIENT` | 再試行で解決しうる | 入力を破壊せず通知（トースト等） |

- 分類は**命名から機械的に決まらない**。実際の `raise` 箇所を見て「ユーザーが何をすれば解決するか」で判断する（例: `SEGMENT_CSV_URL_MISSING` は名前に `MISSING` を含むが内部要因なので `TRANSIENT`、`FILE_DELETE_NOT_ALLOWED` は入力修正で解決しないので `VALIDATION` ではない）
- `UNKNOWN` は `TRANSIENT`。未分類の失敗が「入力を直せば解決する」ように見えてはならない
- テンプレート統合・インライン表示化は Issue #117 のステップ3以降

#### TRANSIENT の表示（実装済み）

再試行で解決しうるエラーは**画面を書き換えず** `toast_response(e)` を返す。

```python
except PersonaManagerError as e:
    logger.warning("...", exc_info=True)
    if is_transient(e):
        return toast_response(e)          # 入力を保持したままトースト通知
    return templates.TemplateResponse(...)  # VALIDATION 等は従来どおり
```

- Manager層の例外型は VALIDATION と TRANSIENT の**両方**を投げるため、`except` 節は型では区別できない。判断は `is_transient()` / `is_correctable()` に集約する（Routerに kind 分岐を散らさない）
- `toast_response()` は本文を返さず `HX-Trigger` ヘッダーでクライアントに通知する。htmx は `HX-Trigger` をスワップ判定より前に処理するため、**4xxでも動作する**（htmx 1.9.10 は4xx本文をスワップしないという制約を受けない）
- HTTPヘッダーは latin-1 のみなので、文言は `json.dumps` の既定（`ensure_ascii=True`）で `\uXXXX` にエスケープする。`ensure_ascii=False` にすると `UnicodeEncodeError` になる
- クライアント側は `app.js` の `showToast` リスナーが `showFlashMessage()` に委譲する
- **変更系（POST/PUT/DELETE）の `except Exception`（総称ハンドラ）はエラーパーシャルを返してはならない。** コードを持たない例外は TRANSIENT に落ちるため `toast_response()` を使う（`tests/api/test_error_exposure.py` の `TestGenericExceptionsDoNotReplaceContent` が機械検査する）

#### 非2xx応答を画面に届ける（実装済み）

htmx 1.9.10 は `status>=200 && status<400 && status!==204` 以外の本文をスワップしない。**4xx/5xx でエラーパーシャルを返す場合は `mark_renderable()` で印を付ける。**付けないと文言は生成されているのに画面へ届かず、`app.js` の汎用フォールバックだけが出る。

```python
return mark_renderable(
    templates.TemplateResponse(
        request, "partials/error_inline.html",
        {"request": request, "error": user_message_for(e)},
        status_code=400,
    )
)
```

- ステータスコードで一律にスワップを許可**してはならない**。汎用パーシャルが `hx-target`（本体コンテンツや一覧）に流れ込むとフォームごと消える経路がある。「表示してよい」判断はサーバー側が持つ
- `tests/api/test_error_exposure.py` の `TestErrorPartialsReachTheScreen` が、非2xxでエラーパーシャルを返す全箇所に印が付いていることをASTで検査する
- `HX-Retarget` は**単独では効かない**（スワップ判定より前に処理されるが `shouldSwap` は false のまま）。差し替え先を変える場合も `mark_renderable()` を併用する

#### VALIDATION / CAPACITY の表示（実装済み）

入力を直せば解決するエラーは**送信値を保持**する。判断は `is_correctable()`（VALIDATION と CAPACITY の両方）。

2つの方式があり、フォームが Alpine 管理下かどうかで選ぶ。

| 方式 | 使う場面 | 例 |
|---|---|---|
| フォーム再描画 | 素のHTMLフォーム | `persona/partials/edit_form.html`（送信値を `form` で渡し `persona` にフォールバック） |
| `HX-Retarget` で専用領域だけ差し替え | Alpine が表示状態を持つフォーム | 知識追加（`find .memory-form-error`）。再描画すると `x-show` が初期値に戻り入力欄が閉じる |

- フィールド単位の表示は `web/templates/components/form_errors.html` のマクロ（`field_error` / `field_border` / `form_error_summary`）を使う。対象フィールドは `field_of(e)` で取得する
- **Jinjaテンプレートで送信値を参照するときは `f['key']` 形式を使う。** `f.values` / `f.items` / `f.keys` は dict のメソッドに解決され、入力値が消える
- **Jinjaの注釈（`{# #}`）内にタグ記法を書いてはならない。** コメントでも解析され未定義エラーになる
- `HX-Retarget` の差し替え先を **絶対 id にしてはならない**。同じ領域を持つフォームが複数同時にDOM上へ存在しうる（手動入力タブ / ファイルプレビュー）と id が重複し、送信元と別のフォームが選ばれてエラーが見えなくなる。`find <セレクタ>`（送信元要素からの相対解決）を使い、差し替え先は各 form の内側に置く
- `HX-Retarget` の差し替え先が各フォーム内に存在することを `TestFormErrorTargetExists` が検査する

### エラー表示テンプレート

**2種のみ。** 機能別に増やしてはならない。変数名は `error` に統一する（`message` は使わない）。

| テンプレート | 用途 | 特徴 |
|---|---|---|
| `partials/error_inline.html` | フォーム近傍（VALIDATION / CAPACITY） | **再試行の導線を持たない**（更新すると直そうとしている入力が破棄される） |
| `partials/error_banner.html` | 領域置換（GET の読み込み失敗 / NOT_FOUND） | 再試行ボタン + 任意の復帰リンク（`back_url` / `back_label`） |

- 例外: htmx のスワップ契約（差し替え先ID・領域クリア）を持つ2種は外側を各テンプレートに残し、見た目だけ `components/error_body.html` のマクロで揃える（`memory_delete_error.html` / `knowledge_file_error.html`）
- htmx のイベントハンドラは `web/static/js/app.js` に集約する。`base.html` に登録してはならない（二重登録で `fade-in` が重複適用される）
- 上記は `TestErrorTemplatesAreConsolidated` / `TestHtmxHandlersAreNotDuplicated` が検査する

### リクエスト検証エラー（422）

`Form(...)` / Pydanticボディの検証失敗は FastAPI が Router に入る**前**に検出するため、Router内の `except` では捕捉できずカタログを経由しない。`web/main.py` のグローバルハンドラ（`RequestValidationError`）で処理する。

- htmx リクエスト（`HX-Request` ヘッダー）には **`toast_response()` でトースト通知**し、それ以外はFastAPI標準のJSON応答を維持する（`web/routers/api.py` のJSONクライアント互換のため）
- **パーシャルHTMLを返してはならない。** このハンドラは全フォーム共通で発火元の `hx-target` を知らないため、本文を返すとペルソナ編集などで本体コンテナへスワップされフォームと入力値が消える（#117 原因B）。トーストならDOMを書き換えず入力を保持できる
- `exc.errors()` の `input` には利用者の入力値が載る。**レスポンスに転写してはならない**（Router層で `str(e)` を書かない原則と同じ扱い）
- Router 個別に `try/except` を足して対処しない（`Form(...)` は40箇所以上あり、分散させるとこの穴が再発する）

### 検査

- `tests/api/test_error_exposure.py` が `web/routers/` をASTで走査し、例外変数がログ・`user_message_for()` 以外から参照されていないことを機械的に検査する
- 同ファイルの `TestRequestValidationErrorHandling` が422のグローバルハンドラの登録と挙動（文言・入力値の非転写・JSON経路の維持）を検査する
- `tests/unit/test_error_messages.py` が全 `ErrorCode` のカタログ登録漏れを検知する
- `tests/unit/test_error_kinds.py` が分類の一貫性（全コードが `kind` を持つ、命名と分類の矛盾、`StrEnum` 意味論の維持）を検査する
- テストで文言をアサートしない。`tests/error_helpers.raises_code()` でエラーコードを検証する

詳細な設計背景は `docs/note/exception-message-design.md` を参照。

## テスト

- マーカー: `unit`(src/managers), `integration`(src/services), `api`(web/routers)
- 外部サービスモック: DynamoDB/S3は`moto`、AI系は`unittest.mock.Mock`
- Manager層テスト: コンストラクタDIでモック注入
- Router層テスト: `reset_singletons` autouseフィクスチャでテスト間分離
