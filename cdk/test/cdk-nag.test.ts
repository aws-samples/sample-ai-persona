import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { AwsCustomResource } from 'aws-cdk-lib/custom-resources';
import { applyNagChecks, createClosedDevStage, createDevStage, DevStageOverrides } from '../lib/dev-stage';
import { closedContext, newApp } from './helpers/stages';
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
 * Stage を 3 通り（公開の 2 通りと U1 閉域の Stage）synth し、cdk-nag の違反が 0 件であること（error が 1 件でも残ると
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
const PUBLIC = 'AIPersonaDev';
const CLOSED = 'AIPersonaDevClosed';

/** AwsCustomResource のシングルトンの Lambda の construct ID（CDK の公開の定数から作る）。 */
const CUSTOM_RESOURCE_PROVIDER = `AWS${AwsCustomResource.PROVIDER_FUNCTION_UUID.replace(/-/g, '')}`;

const on = (resourcePath: string, ...rules: string[]): Suppression[] =>
  rules.map((rule) => `${resourcePath}|AwsSolutions-${rule}`);
const INFERENCE = [
  `IAM5[Resource::arn:aws:bedrock:*:${ACCOUNT}:application-inference-profile/*]`,
  `IAM5[Resource::arn:aws:bedrock:*:${ACCOUNT}:inference-profile/*]`,
  'IAM5[Resource::arn:aws:bedrock:*::foundation-model/*]',
];
const taskPolicy = (stage: string) => `${stage}/App/App/TaskDefinition/TaskRole/DefaultPolicy/Resource`;
/** アンケートのモデル（global の Haiku 5.5。リージョンを * にする 2 つ、基盤設計 3.1）。 */
const SURVEY_MODEL = [
  `IAM5[Resource::arn:aws:bedrock:*:${ACCOUNT}:inference-profile/global.anthropic.claude-haiku-5-5]`,
  'IAM5[Resource::arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5]',
];
/**
 * The state machine's name as cdk-nag prints a GetAtt in a rule ID (`<LogicalId.Name>`). The
 * logical ID comes from the construct path inside the App stack, so it is the same in every Stage.
 */
const MACHINE_NAME = '<SurveyRunStateMachine453666ED.Name>';
/** Executions of the survey state machine (task role: start, describe, stop). */
const SURVEY_EXECUTIONS =
  `IAM5[Resource::arn:aws:states:ap-northeast-1:${ACCOUNT}:execution:${MACHINE_NAME}:*]`;
/** Child executions of the Distributed Map (the state machine's own role). */
const SURVEY_MAP_RUNS =
  `IAM5[Resource::arn:aws:states:ap-northeast-1:${ACCOUNT}:execution:${MACHINE_NAME}/*]`;

/**
 * 公開・閉域の Stage に共通の抑止（infrastructure-specification.md「cdk-nag の指摘と扱い」）。
 * VPC と CloudFront の抑止は Stage ごとに違うため、下の一覧で足す。
 */
function commonSuppressions(stage: string): Suppression[] {
  const DATA = `${stage}/Data`;
  const APP = `${stage}/App`;
  /** IAM5 of a resource string built from a value the Data stack exports. */
  const fromData = (resourcePath: string, attribute: string, suffix: string) =>
    `IAM5[Resource::${exportedRef(`${DATA}/${resourcePath}`, attribute)}${suffix}]`;
  const bucket = (suffix: string) => fromData('UploadBucket/Bucket/Resource', 'Arn', suffix);
  return [
    // Data スタック
    ...on(`${DATA}/UploadBucket/Bucket/Resource`, 'S1'),
    ...on(`${DATA}/Cognito/UserPool/Resource`, 'COG2', 'COG8'),
    ...on(`${DATA}/TokenEncryptionKeys/Resource`, 'SMG4'),
    ...on(`${DATA}/LoginCookieSigningKey/Resource`, 'SMG4'),
    // App スタック
    ...on(`${APP}/App/Alb/Resource`, 'ELB2'),
    ...on(`${APP}/App/Cluster/Resource`, 'ECS4'),
    ...on(`${APP}/App/TaskDefinition/Resource`, 'ECS2'),
    ...on(`${APP}/CognitoClientSecret/Resource`, 'SMG4'),
    ...on(
      `${APP}/${CUSTOM_RESOURCE_PROVIDER}/ServiceRole/Resource`,
      'IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]',
    ),
    ...on(`${APP}/App/TaskDefinition/ExecutionRole/DefaultPolicy/Resource`, 'IAM5[Resource::*]'),
    // テーブルの GSI は名前で付けるため、/index/* の抑止はない
    ...on(
      taskPolicy(stage),
      'IAM5[Resource::*]',
      ...['/*', '/uploads/*', '/candidates/*'].map(bucket),
      ...INFERENCE,
      `IAM5[Resource::arn:aws:bedrock:ap-northeast-1:${ACCOUNT}:knowledge-base/*]`,
      fromData('Memory/Default', 'MemoryArn', '/*'),
      'IAM5[Action::kms:GenerateDataKey*]',
      'IAM5[Action::kms:ReEncrypt*]',
      // アンケートの実行（このステートマシンの実行の ARN、基盤設計 3.2）
      SURVEY_EXECUTIONS,
    ),
    // アンケートのステートマシン（基盤設計 2.2・3.1）: ログは ERROR だけ、トレースは使わない
    ...on(`${APP}/SurveyRun/StateMachine/Resource`, 'SF1', 'SF2'),
    ...on(
      `${APP}/SurveyRun/Role/DefaultPolicy/Resource`,
      'IAM5[Resource::*]',
      ...['/survey-runs/*', '/survey-runs/*/results/*'].map(bucket),
      ...SURVEY_MODEL,
    ),
    ...on(`${APP}/SurveyRun/ExecutionPolicy/Resource`, SURVEY_MAP_RUNS),
  ];
}

/** 公開の Stage の VPC（NAT あり）と CloudFront。 */
const PUBLIC_SUPPRESSIONS: Suppression[] = [
  ...commonSuppressions(PUBLIC),
  ...on(`${PUBLIC}/App/Vpc/Vpc/Resource`, 'VPC7'),
  ...on(`${PUBLIC}/App/CloudFront/Distribution/Resource`, 'CFR1', 'CFR3', 'CFR4'),
];

/** 既定の dev の値（enableWaf: false）: WebACL がないため CFR2 も抑止する（利用者の指示）。 */
const DEFAULT_SUPPRESSIONS: Suppression[] = [
  ...PUBLIC_SUPPRESSIONS,
  ...on(`${PUBLIC}/App/CloudFront/Distribution/Resource`, 'CFR2'),
];

/** 追加のペルソナモデルと WAF を有効にした値: CFR2 は出ず、Mantle の project が増える。 */
const MANTLE_WAF_SUPPRESSIONS: Suppression[] = [
  ...PUBLIC_SUPPRESSIONS,
  ...on(taskPolicy(PUBLIC), `IAM5[Resource::arn:aws:bedrock-mantle:*:${ACCOUNT}:project/*]`),
];

/**
 * U1 閉域の Stage（検証用 EC2 あり）: CloudFront がなく、閉域の VPC の Flow Logs は U2 までの
 * 一時的な抑止。検証用 EC2 の詳細監視・終了保護と、ロールの AmazonSSMManagedInstanceCore
 * （security-design.md §7）。EBS は暗号化するため EC26 は出ない。
 */
const CLOSED_SUPPRESSIONS: Suppression[] = [
  ...commonSuppressions(CLOSED),
  // AZ の固定（W3010、aws-cdk-lib の CloudFormation Validate。Infrastructure Design レビュー R-05）
  ...[
    `${CLOSED}/App/Network/Vpc/IsolatedSubnet1/Subnet`,
    `${CLOSED}/App/Network/Vpc/IsolatedSubnet2/Subnet`,
    `${CLOSED}/App/Validation/Instance/Resource`,
  ].map((resourcePath) => `${resourcePath}|CloudFormation-Validate::W3010`),
  ...on(`${CLOSED}/App/Network/Vpc/Resource`, 'VPC7'),
  ...on(`${CLOSED}/App/Validation/Instance/Resource`, 'EC28', 'EC29'),
  ...on(
    `${CLOSED}/App/Validation/Role/Resource`,
    'IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonSSMManagedInstanceCore]',
  ),
];

interface Variant {
  name: string;
  build: () => cdk.App;
  stackNames: string[];
  expected: Suppression[];
}

const PUBLIC_STACKS = [`${PUBLIC}-App`, `${PUBLIC}-Data`, `${PUBLIC}-Edge`];

const VARIANTS: Variant[] = [
  { name: '既定の dev の値', build: () => build({}), stackNames: PUBLIC_STACKS, expected: DEFAULT_SUPPRESSIONS },
  {
    name: 'enableAdditionalPersonaModels: true・enableWaf: true',
    build: () => build({ enableAdditionalPersonaModels: true, enableWaf: true }),
    stackNames: PUBLIC_STACKS,
    expected: MANTLE_WAF_SUPPRESSIONS,
  },
  {
    name: '閉域の Stage（検証用 EC2 あり）',
    build: buildClosed,
    stackNames: [`${CLOSED}-App`, `${CLOSED}-Data`],
    expected: CLOSED_SUPPRESSIONS,
  },
];

function build(overrides: DevStageOverrides): cdk.App {
  const app = new cdk.App({ context: { ...FEATURE_FLAGS, ...PATH_METADATA_CONTEXT } });
  createDevStage(app, { account: ACCOUNT, ...overrides });
  applyNagChecks(app);
  return app;
}

/** The closed Stage only, from the `-c` context deploy.sh --closed passes (not synthesized yet). */
function buildClosed(): cdk.App {
  const app = newApp(closedContext());
  createClosedDevStage(app, { account: ACCOUNT });
  applyNagChecks(app);
  return app;
}

/**
 * Collect `<construct path>|<rule id>` and the reasons from the template Metadata.
 *
 * Export names inside rule IDs are rewritten with the exporting templates' Outputs, so an
 * unknown or renamed export stays visible in the key and fails the comparison.
 */
function suppressionsOf(
  assembly: cdk.cx_api.CloudAssembly,
  stackNames: string[],
): { keys: string[]; reasons: string[] } {
  // Stage のスタックは入れ子のアセンブリにある
  const stacks = [assembly, ...assembly.nestedAssemblies.map((n) => n.nestedAssembly)].flatMap(
    (a) => a.stacks,
  );
  expect(stacks.map((s) => s.stackName).sort()).toEqual(stackNames);
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

describe.each(VARIANTS)('cdk-nag（$name）', ({ build: buildApp, stackNames, expected }) => {
  let assembly: cdk.cx_api.CloudAssembly;

  test('app.synth() が例外を投げない（AwsSolutions の違反が 0 件）', () => {
    const app = buildApp();
    expect(() => {
      assembly = app.synth();
    }).not.toThrow();
  });

  test('抑止の一覧が期待と一致し、理由が 10 文字以上', () => {
    expect(assembly).toBeDefined();
    const { keys, reasons } = suppressionsOf(assembly, stackNames);
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
