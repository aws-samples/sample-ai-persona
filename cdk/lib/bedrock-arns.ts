import { IConstruct } from 'constructs';
import { Stack } from 'aws-cdk-lib';

/**
 * 推論で呼べるモデルの ARN（U9、NFR7.16、security-design.md「文の分け方」）。
 *
 * - 基盤モデル: リージョンを `*`（グローバルな推論プロファイルはどのリージョンの基盤モデルにも
 *   振り分ける。IAM の `*` は空の文字列にも一致し、リージョンのない ARN にも効く）
 * - 推論プロファイルとアプリケーション推論プロファイル: このアカウントのもの。リージョンは `*`
 *   （リージョン間の推論プロファイルの ARN は呼んだリージョンを持つ）
 *
 * モデルを変えても CDK を直さずに済む。タスクロール（推論・バッチ推論の作成）と
 * バッチ推論ロール（InvokeModel）で同じ一覧を使う。
 */
export function bedrockInferenceArns(scope: IConstruct): string[] {
  const { partition, account } = Stack.of(scope);
  return [
    `arn:${partition}:bedrock:*::foundation-model/*`,
    `arn:${partition}:bedrock:*:${account}:inference-profile/*`,
    `arn:${partition}:bedrock:*:${account}:application-inference-profile/*`,
  ];
}
