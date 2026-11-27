import { IConstruct } from 'constructs';
import { Stack } from 'aws-cdk-lib';
import { JP_REGIONS, ModelIds, PUBLIC_PROFILE, ResolvedProfile } from './deployment-profile';

/** The parts of a ResolvedProfile that decide the inference ARNs. */
export type InferenceArnInput = Pick<ResolvedProfile, 'inferenceScope' | 'explicitModelIds'>;

/** The Stack values the ARNs are built from. */
export interface InferenceArnEnvironment {
  partition: string;
  account: string;
  region: string;
}

/** Inference profile ID prefixes; anything else is a bare foundation model ID. */
const INFERENCE_PROFILE_ID = /^(global|us|us-gov|eu|apac|au|ca)\.(.+)$/;

/**
 * ARNs of the models that inference may call.
 *
 * global:
 * - Foundation models: region `*` (a global inference profile routes to a foundation model in
 *   any region; IAM `*` also matches an empty string, so ARNs without a region are covered).
 * - Inference profiles and application inference profiles: this account's, region `*`
 *   (a cross-region profile ARN carries the calling region).
 *
 * jp: foundation models in the two domestic regions, `jp.*` inference profiles in the Stack's
 * region, and this account's application inference profiles (what they point to is controlled
 * operationally). Explicit model IDs other than `jp.*` are recognized by shape and their ARNs
 * are added. Duplicate ARNs are merged.
 *
 * The task role (inference) and the cdk-nag suppression share this list, and the closed-network
 * Bedrock endpoint policies are built from it too (endpoint-policies.ts). The survey model is
 * invoked by the state machine, so it is not included here (see surveyModelArns).
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
    // The survey model is invoked by the state machine (surveyModelArns), not by the task
    if (modelId.startsWith('jp.') || name === 'surveyModelId') {
      continue;
    }
    arns.push(...explicitModelArns(env, name, modelId));
  }
  return [...new Set(arns)];
}

/** `global.<model>` / `jp.<model>`: the only shapes a survey model ID may take. */
const SURVEY_MODEL_ID = /^(global|jp)\.(.+)$/;

/**
 * ARNs the survey state machine may call with InvokeModel.
 *
 * - `global.<model>`: this account's inference profile (region `*`) and the foundation model
 *   it routes to (region `*`)
 * - `jp.<model>`: the inference profile in the Stack's region and the foundation model in the
 *   two domestic regions
 *
 * Narrower than the task role's list (inferenceArnList). Any other prefix or an ARN is an
 * error, so the callable scope is never widened silently.
 */
export function surveyModelArns(env: InferenceArnEnvironment, surveyModelId: string): string[] {
  const { partition, account, region } = env;
  const match = SURVEY_MODEL_ID.exec(surveyModelId);
  if (!match || surveyModelId.startsWith('arn:')) {
    throw new Error(
      `surveyModelId の "${surveyModelId}" は使えない（global.* か jp.* の推論プロファイルの ID で渡す）`,
    );
  }
  const [, scope, model] = match;
  if (scope === 'global') {
    return [
      `arn:${partition}:bedrock:*:${account}:inference-profile/${surveyModelId}`,
      `arn:${partition}:bedrock:*::foundation-model/${model}`,
    ];
  }
  return [
    `arn:${partition}:bedrock:${region}:${account}:inference-profile/${surveyModelId}`,
    ...JP_REGIONS.map((jpRegion) => `arn:${partition}:bedrock:${jpRegion}::foundation-model/${model}`),
  ];
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
