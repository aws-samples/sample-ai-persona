import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AIPersonaStage } from '../../lib/ai-persona-stage';
import {
  applyNagChecks,
  createClosedDevStage,
  createDevStage,
  DevStageOverrides,
} from '../../lib/dev-stage';
import { PATH_METADATA_CONTEXT } from '../template-paths';

/**
 * テストで Stage を組み立てる共通の補助（U1 閉域、security-design.md §6）。
 *
 * bin/app.ts と同じ関数（createDevStage・createClosedDevStage・applyNagChecks）を通し、
 * cdk.json の機能フラグと construct のパスのメタデータを入れた App で組み立てる。閉域のテストの
 * 値（証明書の ARN・FQDN・許可 CIDR・検証用 EC2）はここにだけ持つ。
 */

export const ACCOUNT = '123456789012';
export const REGION = 'ap-northeast-1';

/** 閉域のテストの値（形だけの値。CI の閉域の synth と同じ。AWS には触れない）。 */
export const CLOSED_TEST_VALUES = {
  certificateArn:
    'arn:aws:acm:ap-northeast-1:123456789012:certificate/00000000-0000-0000-0000-000000000000',
  domainName: 'ai-persona.closed.example',
  allowedCidrs: ['10.10.0.0/16'],
  createTestEnvironment: true,
} as const;

// eslint-disable-next-line @typescript-eslint/no-require-imports
export const FEATURE_FLAGS: Record<string, unknown> = require('../../cdk.json').context;

/** The synthesized templates of one Stage, keyed by stack ID (`Data`, `App`, `Edge`). */
export interface SynthesizedStage {
  app: cdk.App;
  stage: AIPersonaStage;
  templates: Record<string, Template>;
}

/** A fresh App with the feature flags of cdk.json and `aws:cdk:path` metadata. */
export function newApp(context: Record<string, unknown> = {}): cdk.App {
  return new cdk.App({ context: { ...FEATURE_FLAGS, ...PATH_METADATA_CONTEXT, ...context } });
}

/** Templates of every stack of a Stage, keyed by the stack's construct ID. */
export function templatesOf(stage: cdk.Stage): Record<string, Template> {
  const stacks = stage.node.children.filter((child): child is cdk.Stack => cdk.Stack.isStack(child));
  return Object.fromEntries(stacks.map((stack) => [stack.node.id, Template.fromStack(stack)]));
}

/** 閉域の Stage の入力のうち、テストで変えるもの。 */
export interface ClosedTestOverrides {
  certificateArn?: string;
  domainName?: string;
  allowedCidrs?: readonly string[];
  createTestEnvironment?: boolean;
}

/** The `-c` context that deploy.sh --closed passes, built from the closed test values. */
export function closedContext(overrides: ClosedTestOverrides = {}): Record<string, string> {
  const values = { ...CLOSED_TEST_VALUES, ...overrides };
  return {
    closedCertificateArn: values.certificateArn,
    closedDomainName: values.domainName,
    closedAllowedCidrs: values.allowedCidrs.join(','),
    closedCreateTestEnvironment: String(values.createTestEnvironment),
  };
}

/**
 * The closed Stage AIPersonaDevClosed, built from the `-c` context the way bin/app.ts does
 * (createClosedDevStage, then applyNagChecks). Only the closed Stage is in the App.
 */
export function synthClosed(
  overrides: ClosedTestOverrides = {},
  context: Record<string, unknown> = {},
): SynthesizedStage {
  const app = newApp({ ...closedContext(overrides), ...context });
  const stage = createClosedDevStage(app, { account: ACCOUNT });
  if (!stage) {
    throw new Error('createClosedDevStage() did not build the closed Stage from the test context.');
  }
  applyNagChecks(app);
  return { app, stage, templates: templatesOf(stage) };
}

/** The public Stage AIPersonaDev, built and checked the way bin/app.ts does. */
export function synthPublic(
  overrides: DevStageOverrides = {},
  context: Record<string, unknown> = {},
): SynthesizedStage {
  const app = newApp(context);
  const stage = createDevStage(app, { account: ACCOUNT, ...overrides });
  applyNagChecks(app);
  return { app, stage, templates: templatesOf(stage) };
}
