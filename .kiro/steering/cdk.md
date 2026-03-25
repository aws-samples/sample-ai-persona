---
inclusion: manual
---

````markdown
# CDK design

## 1. アーキテクチャ概要

## 2. Stack一覧
* Stack IDのSuffixに`Stack`を付けないでください。

| Stack ID   | 説明   | 依存関係        |
| --------- | ----- | -------------- |
| [StackId] | [説明] | [依存するStack] |

## 3. Construct設計
* Construct IDのSuffixに`Construct`を付けないでください。
* Consructの中のリソースは`Default`と`Resource`を適切に活用しConstruct IDを短縮してください。

| Construce ID　  | 説明   |
| ------------- | ----- |
| [ConstructId] | [説明] |

### 3. パラメータ設計
* パラメータはCDKディレクトリ以下に`parameters.ts`を作成し、以下のように管理します:
```
import { Environment } from 'aws-cdk-lib';

export interface AppParameter {
  env?: Environment
  envName: string
  parameterA: string
}

// Example for Development
export const devParameter: AppParameter = {
  envName: 'Development'
  parameterA: 'hoge'
}
```

| パラメータ名    | 説明   |
| ------------- | ----- |
| [Parameter A] | [説明] |

## 4. 使用するライブラリ
* L2がないConstructの場合、`@aws-cdk/aws-lambda-python-alpha` などのalphaライブラリがないかをMCPサーバーを使用して調べて、ユーザーに利用の提案をします。
- [ライブラリ1]

## 5. ディレクトリ構造
* CDKのディレクトリや、アセット(FrontendやLambdaのコードなど)をどこに配置するか、ユーザーに確認しながら書いてください。

## 6. その他の注意事項
- [注意点1]
```