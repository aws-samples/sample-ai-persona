# dicebear.min.js

議論ステージのペルソナアバター生成用。npm公式パッケージから自前ビルドした自己完結バンドル
（`@dicebear/core@9.4.2` + `@dicebear/notionists@9.4.2`、esbuild で IIFE 化）。
`window.DiceBear = { createAvatar, styles: { notionists } }` を公開する。実行時の外部通信なし。

## 再ビルド手順
バージョン/スタイルを変えるときだけ実行する。

```bash
mkdir dicebear-build && cd dicebear-build && npm init -y
npm i -E @dicebear/core@9.4.2 @dicebear/notionists@9.4.2 esbuild@0.25.0
cat > entry.mjs <<'EOF'
import { createAvatar } from '@dicebear/core';
import * as notionists from '@dicebear/notionists';
const DiceBear = { createAvatar, styles: { notionists } };
if (typeof window !== 'undefined') window.DiceBear = DiceBear;
else if (typeof globalThis !== 'undefined') globalThis.DiceBear = DiceBear;
export default DiceBear;
EOF
npx esbuild entry.mjs --bundle --format=iife --target=es2019 --minify \
  --legal-comments=inline --outfile=dicebear.min.js
```

再ビルドしたら **必ず SRI を再計算して `discussion/setup.html` の `integrity` 属性を更新する**
（不一致だとブラウザがファイルを拒否しアバターが出ない）。

```bash
openssl dgst -sha384 -binary dicebear.min.js | openssl base64 -A
```

## ライセンス
- コード（npmパッケージ）: **MIT**
- アートワーク（Notionists）: **CC0 1.0**
