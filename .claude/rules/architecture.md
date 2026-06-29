# アーキテクチャ制約

## 依存方向（違反厳禁）

Router層 → Manager層 → Service層。Models は全層から参照可。逆方向禁止。

- Models (src/models/): 標準ライブラリのみインポート可。Service/Manager/Routerをインポートしてはならない
- Service層 (src/services/): Manager層・Router層をインポートしてはならない
- Manager層 (src/managers/): Router層をインポートしてはならない。他のManagerをインポートしてはならない
- Shared (src/managers/shared/): Manager層の共有ユーティリティ。Service層・Router層からのインポート禁止。他のManagerからインポート可
- Router層 (web/routers/): Service層を直接使ってはならない（Manager経由で操作する）

## 各層の制約

### Models
- selfを変更するメソッドを定義してはならない（更新は新インスタンスを返す）
- `to_dict()`でNone値のフィールドを含めてはならない
- 既存モデルの`create_new()`, `update()`, `to_dict()`, `from_dict()`パターンに従うこと

### Manager層
- Manager固有の例外クラスを定義し、Service層の例外をキャッチして変換すること
- コンストラクタでServiceをオプション引数として受け取り、未指定時は`service_factory`から取得すること
- ビジネスロジック（バリデーション、ワークフロー制御）はこの層に書くこと
- HTTP通信・ファイルI/O・データフレーム操作・boto3直接呼び出しを書いてはならない（Service層に委譲）

### Service層
- 環境変数は`src/config.py`経由で参照すること。直接`os.environ`を使ってはならない
- Service固有の例外クラスを定義すること
- リトライ・タイムアウト処理はこの層に閉じること
- ビジネスルール判定（件数上下限、ステータス遷移等）を書いてはならない（Manager層に委譲）

### Router層
- ビジネスロジックを書いてはならない（Manager層に委譲）
- Managerはモジュールレベル変数 + `get_*_manager()`遅延初期化で保持すること
- 同期的なAI/DB処理は`ThreadPoolExecutor`で非同期化すること

## テスト

- マーカー: `unit`(src/managers), `integration`(src/services), `api`(web/routers)
- 外部サービスモック: DynamoDB/S3は`moto`、AI系は`unittest.mock.Mock`
- Manager層テスト: コンストラクタDIでモック注入
- Router層テスト: `reset_singletons` autouseフィクスチャでテスト間分離
