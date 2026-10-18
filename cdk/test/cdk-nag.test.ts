import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { AwsCustomResource } from 'aws-cdk-lib/custom-resources';
import { applyNagChecks, createDevStage, DevStageOverrides } from '../lib/dev-stage';
import {
  describeExports,
  exportedRef,
  exportTargets,
  PATH_METADATA_CONTEXT,
  pathOf,
} from './template-paths';

/**
 * cdk-nag の AwsSolutions（U9、NFR7.15、NFR3.14、Infrastructure Design Q1 A）。
 *
 * Stage を 2 通り synth し、cdk-nag の違反が 0 件であること（error が 1 件でも残ると
 * app.synth() が ValidationFailed を投げる）と、抑止の一覧が期待と一致することを確かめる。
 * cdk-nag を掛けるのは bin/app.ts とこのテストだけ。抑止を足す・消す変更は、同じ変更で
 * 下の期待の一覧を直す。
 */

jest.setTimeout(60_000);

// eslint-disable-next-line @typescript-eslint/no-require-imports
const FEATURE_FLAGS: Record<string, unknown> = require('../cdk.json').context;

/**
 * `<抑止を持つリソースの construct のパス>|<ルール ID>`。ルール ID の中の Data スタックの
 * 出力名は `{<出力するリソースのパス>.<属性>}` に直して比べる（CDK が作る名前を書かない）。
 */
type Suppression = string;

const ACCOUNT = '123456789012';
const DATA = 'AIPersonaDev/Data';
const APP = 'AIPersonaDev/App';
const STACK_NAMES = ['AIPersonaDev-App', 'AIPersonaDev-Data', 'AIPersonaDev-Edge'];

/** AwsCustomResource のシングルトンの Lambda の construct ID（CDK の公開の定数から作る）。 */
const CUSTOM_RESOURCE_PROVIDER = `AWS${AwsCustomResource.PROVIDER_FUNCTION_UUID.replace(/-/g, '')}`;

const on = (resourcePath: string, ...rules: string[]): Suppression[] =>
  rules.map((rule) => `${resourcePath}|AwsSolutions-${rule}`);
/** IAM5 of a resource string built from a value the Data stack exports. */
const fromData = (resourcePath: string, attribute: string, suffix: string) =>
  `IAM5[Resource::${exportedRef(`${DATA}/${resourcePath}`, attribute)}${suffix}]`;
const bucket = (suffix: string) => fromData('UploadBucket/Bucket/Resource', 'Arn', suffix);
const INFERENCE = [
  `IAM5[Resource::arn:aws:bedrock:*:${ACCOUNT}:application-inference-profile/*]`,
  `IAM5[Resource::arn:aws:bedrock:*:${ACCOUNT}:inference-profile/*]`,
  'IAM5[Resource::arn:aws:bedrock:*::foundation-model/*]',
];
const TASK_POLICY = `${APP}/App/TaskDefinition/TaskRole/DefaultPolicy/Resource`;

/** 2 通りに共通の抑止（infrastructure-specification.md「cdk-nag の指摘と扱い」）。 */
const COMMON_SUPPRESSIONS: Suppression[] = [
  // Data スタック
  ...on(`${DATA}/UploadBucket/Bucket/Resource`, 'S1'),
  ...on(`${DATA}/Cognito/UserPool/Resource`, 'COG2', 'COG8'),
  ...on(`${DATA}/TokenEncryptionKeys/Resource`, 'SMG4'),
  ...on(`${DATA}/LoginCookieSigningKey/Resource`, 'SMG4'),
  // App スタック
  ...on(`${APP}/Vpc/Vpc/Resource`, 'VPC7'),
  ...on(`${APP}/App/Alb/Resource`, 'ELB2'),
  ...on(`${APP}/App/Cluster/Resource`, 'ECS4'),
  ...on(`${APP}/App/TaskDefinition/Resource`, 'ECS2'),
  ...on(`${APP}/CloudFront/Distribution/Resource`, 'CFR1', 'CFR3', 'CFR4'),
  ...on(`${APP}/CognitoClientSecret/Resource`, 'SMG4'),
  ...on(
    `${APP}/${CUSTOM_RESOURCE_PROVIDER}/ServiceRole/Resource`,
    'IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]',
  ),
  ...on(`${APP}/App/TaskDefinition/ExecutionRole/DefaultPolicy/Resource`, 'IAM5[Resource::*]'),
  // テーブルの GSI は名前で付けるため、/index/* の抑止はない
  ...on(
    TASK_POLICY,
    'IAM5[Resource::*]',
    ...['/*', '/uploads/*', '/candidates/*'].map(bucket),
    ...INFERENCE,
    `IAM5[Resource::arn:aws:bedrock:ap-northeast-1:${ACCOUNT}:knowledge-base/*]`,
    `IAM5[Resource::arn:aws:bedrock:ap-northeast-1:${ACCOUNT}:model-invocation-job/*]`,
    fromData('Memory/Default', 'MemoryArn', '/*'),
    'IAM5[Action::kms:GenerateDataKey*]',
    'IAM5[Action::kms:ReEncrypt*]',
  ),
  ...on(
    `${APP}/BedrockBatchRole/Role/DefaultPolicy/Resource`,
    ...['/batch-inference/*', '/batch-inference/output/*'].map(bucket),
    ...INFERENCE,
  ),
];

/** 既定の dev の値（enableWaf: false）: WebACL がないため CFR2 も抑止する（利用者の指示）。 */
const DEFAULT_SUPPRESSIONS: Suppression[] = [
  ...COMMON_SUPPRESSIONS,
  ...on(`${APP}/CloudFront/Distribution/Resource`, 'CFR2'),
];

/** 追加のペルソナモデルと WAF を有効にした値: CFR2 は出ず、Mantle の project が増える。 */
const MANTLE_WAF_SUPPRESSIONS: Suppression[] = [
  ...COMMON_SUPPRESSIONS,
  ...on(TASK_POLICY, `IAM5[Resource::arn:aws:bedrock-mantle:*:${ACCOUNT}:project/*]`),
];

interface Variant {
  name: string;
  overrides: DevStageOverrides;
  expected: Suppression[];
}

const VARIANTS: Variant[] = [
  { name: '既定の dev の値', overrides: {}, expected: DEFAULT_SUPPRESSIONS },
  {
    name: 'enableAdditionalPersonaModels: true・enableWaf: true',
    overrides: { enableAdditionalPersonaModels: true, enableWaf: true },
    expected: MANTLE_WAF_SUPPRESSIONS,
  },
];

function build(overrides: DevStageOverrides): cdk.App {
  const app = new cdk.App({ context: { ...FEATURE_FLAGS, ...PATH_METADATA_CONTEXT } });
  createDevStage(app, { account: ACCOUNT, ...overrides });
  applyNagChecks(app);
  return app;
}

/**
 * Collect `<construct path>|<rule id>` and the reasons from the template Metadata.
 *
 * Export names inside rule IDs are rewritten with the exporting templates' Outputs, so an
 * unknown or renamed export stays visible in the key and fails the comparison.
 */
function suppressionsOf(assembly: cdk.cx_api.CloudAssembly): { keys: string[]; reasons: string[] } {
  // Stage のスタックは入れ子のアセンブリにある
  const stacks = [assembly, ...assembly.nestedAssemblies.map((n) => n.nestedAssembly)].flatMap(
    (a) => a.stacks,
  );
  expect(stacks.map((s) => s.stackName).sort()).toEqual(STACK_NAMES);
  const targets = new Map(stacks.flatMap((stack) => [...exportTargets(stack.template)]));
  expect(targets.size).toBeGreaterThan(0);
  const keys: string[] = [];
  const reasons: string[] = [];
  for (const stack of stacks) {
    for (const resource of Object.values((stack.template.Resources ?? {}) as Record<string, any>)) {
      const rules = resource.Metadata?.cdk_nag?.rules_to_suppress ?? [];
      for (const rule of rules as { id: string; reason: string }[]) {
        keys.push(`${pathOf(resource)}|${describeExports(rule.id, targets)}`);
        reasons.push(rule.reason);
      }
    }
  }
  return { keys: keys.sort(), reasons };
}

describe.each(VARIANTS)('cdk-nag（$name）', ({ overrides, expected }) => {
  let assembly: cdk.cx_api.CloudAssembly;

  test('app.synth() が例外を投げない（AwsSolutions の違反が 0 件）', () => {
    const app = build(overrides);
    expect(() => {
      assembly = app.synth();
    }).not.toThrow();
  });

  test('抑止の一覧が期待と一致し、理由が 10 文字以上', () => {
    expect(assembly).toBeDefined();
    const { keys, reasons } = suppressionsOf(assembly);
    expect(keys).toEqual([...expected].sort());
    for (const reason of reasons) {
      expect(reason.trim().length).toBeGreaterThanOrEqual(10);
    }
  });

  test('validation-report.json の違反（warning を含む）が 0 件', () => {
    expect(assembly).toBeDefined();
    // 抑止がそろった状態でも出力先に書かれる（cdk-nag 3.0.2 で確かめた）
    const report = path.join(assembly.directory, 'validation-report.json');
    expect(fs.existsSync(report)).toBe(true);
    const json = JSON.parse(fs.readFileSync(report, 'utf-8'));
    const violations = (json.pluginReports ?? []).flatMap((p: any) => p.violations ?? []);
    expect(violations).toEqual([]);
  });
});
