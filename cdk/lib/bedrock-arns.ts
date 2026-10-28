import { IConstruct } from 'constructs';
import { Stack } from 'aws-cdk-lib';
import { JP_REGIONS, ModelIds, PUBLIC_PROFILE, ResolvedProfile } from './deployment-profile';

/** The parts of a ResolvedProfile that decide the inference ARNs (U5). */
export type InferenceArnInput = Pick<ResolvedProfile, 'inferenceScope' | 'explicitModelIds'>;

/** The Stack values the ARNs are built from. */
export interface InferenceArnEnvironment {
  partition: string;
  account: string;
  region: string;
}

/** 推論プロファイルの ID の接頭辞（それ以外は接頭辞のない基盤モデルの ID、BR2.3）。 */
const INFERENCE_PROFILE_ID = /^(global|us|us-gov|eu|apac|au|ca)\.(.+)$/;

/**
 * 推論で呼べるモデルの ARN（U9、NFR7.16、security-design.md「文の分け方」。閉域対応 U5 で範囲を足した）。
 *
 * global（BR2.1、今のまま）:
 * - 基盤モデル: リージョンを `*`（グローバルな推論プロファイルはどのリージョンの基盤モデルにも
 *   振り分ける。IAM の `*` は空の文字列にも一致し、リージョンのない ARN にも効く）
 * - 推論プロファイルとアプリケーション推論プロファイル: このアカウントのもの。リージョンは `*`
 *   （リージョン間の推論プロファイルの ARN は呼んだリージョンを持つ）
 *
 * jp（BR2.2）: 国内の 2 つのリージョンの基盤モデル、Stack のリージョンの `jp.*` の推論プロファイル、
 * このアカウントのアプリケーション推論プロファイル（中身の範囲は運用で守る）。明示した `jp.*` 以外の
 * モデル ID は、形で見分けて呼ぶ ARN を足す（BR2.3）。同じ ARN は 1 つにまとめる。
 *
 * タスクロール（推論・バッチ推論の作成）とバッチ推論ロール（InvokeModel）、cdk-nag の抑止で
 * 同じ一覧を使う（BR2.4）。U2 のエンドポイントのポリシーもこの一覧から作る。
 *
 * Throws when an explicit model ID is given as an ARN under jp (the foundation model behind it
 * cannot be derived from the ARN).
 */
export function inferenceArnList(env: InferenceArnEnvironment, input: InferenceArnInput): string[] {
  const { partition, account, region } = env;
  const applicationProfiles = `arn:${partition}:bedrock:*:${account}:application-inference-profile/*`;
  if (input.inferenceScope === 'global') {
    return [
      `arn:${partition}:bedrock:*::foundation-model/*`,
      `arn:${partition}:bedrock:*:${account}:inference-profile/*`,
      applicationProfiles,
    ];
  }
  const arns = [
    ...JP_REGIONS.map((jpRegion) => `arn:${partition}:bedrock:${jpRegion}::foundation-model/*`),
    `arn:${partition}:bedrock:${region}:${account}:inference-profile/jp.*`,
    applicationProfiles,
  ];
  for (const [name, modelId] of Object.entries(input.explicitModelIds) as [keyof ModelIds, string][]) {
    if (modelId.startsWith('jp.')) {
      continue;
    }
    arns.push(...explicitModelArns(env, name, modelId));
  }
  return [...new Set(arns)];
}

function explicitModelArns(env: InferenceArnEnvironment, name: keyof ModelIds, modelId: string): string[] {
  const { partition, account, region } = env;
  if (modelId.startsWith('arn:')) {
    throw new Error(
      `${name} の "${modelId}" は ARN の形では渡せない（ARN からは基盤モデルの権限を決められないため、` +
        '推論プロファイルの ID か基盤モデルの ID で渡す）',
    );
  }
  const profile = INFERENCE_PROFILE_ID.exec(modelId);
  if (profile) {
    return [
      `arn:${partition}:bedrock:*:${account}:inference-profile/${modelId}`,
      `arn:${partition}:bedrock:*::foundation-model/${profile[2]}`,
    ];
  }
  return [`arn:${partition}:bedrock:${region}::foundation-model/${modelId}`];
}

/** The inference ARNs for the Stack of `scope` (the public global profile when omitted). */
export function bedrockInferenceArns(scope: IConstruct, profile: InferenceArnInput = PUBLIC_PROFILE): string[] {
  const { partition, account, region } = Stack.of(scope);
  return inferenceArnList({ partition, account, region }, profile);
}
