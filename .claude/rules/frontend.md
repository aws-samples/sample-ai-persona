---
paths:
  - "web/templates/**/*.html"
  - "web/static/**/*.js"
  - "web/static/**/*.css"
  - "web/routers/**/*.py"
  - "web/main.py"
---

# フロントエンド規約（htmx / Alpine / Tailwind / Jinja）

UI 全般に効く技術制約。エラー表示への**適用**（TRANSIENT→トースト、VALIDATION→送信値保持等）は
`.claude/rules/error-catalog.md` を参照。ここには「どの場面でも真な基盤知識」だけを置く。

## htmx 1.9.10 のスワップ契約

htmx 1.9.10 は `status>=200 && status<400 && status!==204` **以外の本文をスワップしない**。
4xx/5xx で本文を画面へ届けたい場合はサーバー側で明示的に許可する必要がある。

ヘッダーの処理順（`htmx.min.js` の `Mr(l,u)` を実測）:

```
htmx:beforeOnLoad → HX-Trigger → HX-Location → HX-Refresh → HX-Redirect
→ HX-Retarget → shouldSwap算出 → htmx:beforeSwap → if(shouldSwap) スワップ
                 ↑                ↑
        ここまでは4xxでも動く    ここで4xxは false になる
```

| 機構 | 4xxでの挙動 | 対処 |
|---|---|---|
| `HX-Trigger`（トースト等） | **動く**（スワップ判定より前） | そのまま使える |
| `HX-Retarget` | **単独では効かない**（`shouldSwap` は false のまま） | `mark_renderable()` を併用する |
| 本文のスワップ | しない | `mark_renderable()` で印を付ける |

### `mark_renderable()` / `X-Render-Response`

非2xx応答の本文を画面へ届けるには、サーバーが `X-Render-Response: true` を付けた応答だけを
`app.js` の `htmx:beforeSwap` がスワップ許可する。

- **ステータスコードで一律に許可してはならない。** 汎用パーシャルが `hx-target`（本体コンテンツや一覧）に
  流れ込むとフォームごと消える経路がある。「表示してよい」判断はサーバー側が持つ
- `HX-Retarget` の差し替え先を **絶対 id にしてはならない**。同じ領域を持つフォームが複数同時に
  DOM 上へ存在しうる（手動入力タブ / ファイルプレビュー）と id が重複し、送信元と別のフォームが
  選ばれる。`find <セレクタ>`（送信元要素からの相対解決）を使い、差し替え先は各 form の内側に置く

## htmx イベントハンドラ

- htmx のイベントハンドラは `web/static/js/app.js` に集約する。`base.html` に登録してはならない
  （二重登録で `fade-in` が重複適用される）
- `TestHtmxHandlersAreNotDuplicated` が検査する

## Jinja テンプレート

- **送信値を参照するときは `f['key']` 形式を使う。** `f.values` / `f.items` / `f.keys` は
  dict のメソッドに解決され、入力値が消える
- **注釈（`{# #}`）内にタグ記法を書いてはならない。** コメントでも解析され未定義エラーになる

## Tailwind CSS

- **`web/static/css/tailwind.css` はビルド成果物をコミットしている。** テンプレートや JS でクラスを
  足したら `./scripts/build-css.sh --minify` を実行する。忘れるとクラスが存在せず無効になる
- `TestToastIsVisibleRegardlessOfScroll` がビルド済み CSS にクラスが存在することを検査する

## z-index 階層

- トースト（`#flash-messages`）は `z-toast`(=60)。モーダル（`z-50`）より上に置く。同じ `z-50` だと
  モーダル内のフォーム送信失敗時にモーダルがトーストを覆って文言が見えない
- `#flash-messages` は `<main>` の外に置き `fixed top-20 right-4` で固定。コンテナは
  `pointer-events-none`、トースト自身に `pointer-events-auto`（閉じるボタンを押せるように）
