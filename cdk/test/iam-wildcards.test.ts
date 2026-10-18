import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { createDevStage, DevStageOverrides } from '../lib/dev-stage';
import { exportedRef, exportTargets, flattenReference, PATH_METADATA_CONTEXT } from './template-paths';

/**
 * IAM の `*` と方針の大きさ、秘密の読み手（U9、NFR7.16、NFR3.14、security-design.md）。
 *
 * Stage を 2 通り（既定の dev の値と、追加のペルソナモデルを有効にした値）synth し、
 * 3 スタックの全ロールの方針を調べる。`Resource: "*"` の文の操作は、下の例外の一覧と
 * 完全に一致させる（増えても、減って一覧が古くなっても失敗する）。IAM の文を足す変更は、
 * 同じ変更でこの一覧を直す。
 */

jest.setTimeout(60_000);

const ACCOUNT = '123456789012';
const REGION = 'ap-northeast-1';

/** `Resource: "*"` を残してよい操作と理由。リソースを指定できない API だけ。 */
const WILDCARD_EXCEPTIONS: Record<string, string> = {
  'dynamodb:ListTables': '一覧の API で、リソースを指定できない（起動時の接続の確認）',
  'bedrock:ListFoundationModels': '一覧の API で、リソースを指定できない',
  'bedrock:ListInferenceProfiles': '一覧の API で、リソースを指定できない',
  'bedrock:ListModelInvocationJobs': '一覧の API で、リソースを指定できない',
  'bedrock-agentcore:ListMemories': '一覧の API で、リソースを指定できない',
  'ecr:GetAuthorizationToken': '認可トークンの取得で、リソースを指定できない。CDK が実行ロールに足す',
};
/** enableAdditionalPersonaModels のときだけ足す例外。 */
const MANTLE_WILDCARD_EXCEPTIONS: Record<string, string> = {
  'bedrock-mantle:CallWithBearerToken':
    'リソースの種類を持たない（条件キー bedrock-mantle:BearerTokenType だけ）',
};

/** 推論の 3 つの ARN（基盤モデル・推論プロファイルはリージョンを `*`）。 */
const INFERENCE_ARNS = [
  'arn:aws:bedrock:*::foundation-model/*',
  `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/*`,
  `arn:aws:bedrock:*:${ACCOUNT}:application-inference-profile/*`,
];

const DATA = 'AIPersonaDev/Data';
const tableArn = (id: string) => exportedRef(`${DATA}/Database/${id}/Resource`, 'Arn');

/**
 * タスクロールが Query してよい GSI（アプリが IndexName で引くものだけ、名前で付ける）。
 * OwnerIndex: src/services/resource_share_store.py、TargetIndex: src/services/persona_binding_store.py。
 * 型固定の GSI（NameIndex など）は外す途中で、アプリは使わないため付けない。
 */
const QUERIED_INDEX_ARNS = [
  `${tableArn('Discussions')}/index/OwnerIndex`,
  `${tableArn('PersonaBindings')}/index/TargetIndex`,
  `${tableArn('Personas')}/index/OwnerIndex`,
  `${tableArn('SurveyTemplates')}/index/OwnerIndex`,
  `${tableArn('Surveys')}/index/OwnerIndex`,
];

/** IAM の 1 つの方針の上限（区切りの空白を除いた文字数）。 */
const MAX_INLINE_POLICY = 10_240;
const MAX_MANAGED_POLICY = 6_144;

// eslint-disable-next-line @typescript-eslint/no-require-imports
const FEATURE_FLAGS: Record<string, unknown> = require('../cdk.json').context;

interface Variant {
  name: string;
  overrides: DevStageOverrides;
  expectedWildcards: string[];
}

const VARIANTS: Variant[] = [
  {
    name: '既定の dev の値',
    overrides: {},
    expectedWildcards: Object.keys(WILDCARD_EXCEPTIONS),
  },
  {
    name: 'enableAdditionalPersonaModels: true',
    overrides: { enableAdditionalPersonaModels: true },
    expectedWildcards: [
      ...Object.keys(WILDCARD_EXCEPTIONS),
      ...Object.keys(MANTLE_WILDCARD_EXCEPTIONS),
    ],
  },
];

interface PolicyDoc {
  /** The resource that holds the document (policy logical ID or role logical ID). */
  holder: string;
  type: 'AWS::IAM::Policy' | 'AWS::IAM::ManagedPolicy' | 'AWS::IAM::Role';
  /** Logical IDs of the roles the document applies to. */
  roles: string[];
  document: { Statement: any[] };
}

function templates(overrides: DevStageOverrides): Template[] {
  const stage = createDevStage(
    new cdk.App({ context: { ...FEATURE_FLAGS, ...PATH_METADATA_CONTEXT } }),
    { account: ACCOUNT, ...overrides },
  );
  return [stage.data, stage.app, stage.edge].map((s) => Template.fromStack(s));
}

/** Export name → `{path.attribute}` across the given templates. */
const targetsOf = (all: Template[]) => new Map(all.flatMap((t) => [...exportTargets(t.toJSON())]));

function roleRefs(value: unknown): string[] {
  return ([value].flat().filter(Boolean) as any[]).map((r) => r.Ref ?? JSON.stringify(r));
}

function policyDocs(all: Template[]): PolicyDoc[] {
  const docs: PolicyDoc[] = [];
  for (const t of all) {
    for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy'] as const) {
      for (const [id, r] of Object.entries(t.findResources(type))) {
        docs.push({ holder: id, type, roles: roleRefs(r.Properties.Roles), document: r.Properties.PolicyDocument });
      }
    }
    for (const [id, r] of Object.entries(t.findResources('AWS::IAM::Role'))) {
      for (const p of r.Properties.Policies ?? []) {
        docs.push({ holder: id, type: 'AWS::IAM::Role', roles: [id], document: p.PolicyDocument });
      }
    }
  }
  return docs;
}

const statementsOf = (docs: PolicyDoc[]) => docs.flatMap((d) => d.document.Statement);
const actionsOf = (s: any): string[] => [s.Action].flat().filter(Boolean);
const resourcesOf = (s: any): any[] => [s.Resource].flat().filter((r) => r !== undefined);
const withAction = (docs: PolicyDoc[], action: string) =>
  statementsOf(docs).filter((s) => actionsOf(s).includes(action));

describe.each(VARIANTS)('IAM（$name）', ({ overrides, expectedWildcards }) => {
  const all = templates(overrides);
  const docs = policyDocs(all);
  const targets = targetsOf(all);
  const flat = (s: any) => resourcesOf(s).map((r) => flattenReference(r, targets));

  test('方針が見つかる（空振りしない）', () => {
    expect(docs.length).toBeGreaterThan(3);
  });

  test('Resource が * の文の操作は、例外の一覧と完全に一致する', () => {
    const wildcardActions = new Set(
      statementsOf(docs)
        .filter((s) => resourcesOf(s).includes('*'))
        .flatMap(actionsOf),
    );
    expect([...wildcardActions].sort()).toEqual([...expectedWildcards].sort());
  });

  test('NotAction・NotResource を持つ文がない', () => {
    for (const s of statementsOf(docs)) {
      expect(s.NotAction).toBeUndefined();
      expect(s.NotResource).toBeUndefined();
    }
  });

  test('bedrock:CreateModelInvocationJob はジョブの ARN と推論の 3 つの ARN', () => {
    const statements = withAction(docs, 'bedrock:CreateModelInvocationJob');
    expect(statements.length).toBeGreaterThan(0);
    const resources = statements.flatMap(resourcesOf);
    expect(resources).toEqual(
      expect.arrayContaining([
        `arn:aws:bedrock:${REGION}:${ACCOUNT}:model-invocation-job/*`,
        ...INFERENCE_ARNS,
      ]),
    );
    expect(resources).not.toContain('*');
  });

  test('bedrock:InvokeModel（タスクロールとバッチ推論ロール）は推論の 3 つの ARN だけ', () => {
    const statements = withAction(docs, 'bedrock:InvokeModel');
    const holders = docs
      .filter((d) => d.document.Statement.some((s) => actionsOf(s).includes('bedrock:InvokeModel')))
      .flatMap((d) => d.roles);
    expect(holders.some((r) => r.startsWith('AppTaskDefinitionTaskRole'))).toBe(true);
    expect(holders.some((r) => r.startsWith('BedrockBatchRole'))).toBe(true);
    for (const s of statements) {
      expect([...resourcesOf(s)].sort()).toEqual([...INFERENCE_ARNS].sort());
    }
  });

  test('bedrock:Retrieve はこのアカウントの KB の ARN だけ', () => {
    const statements = withAction(docs, 'bedrock:Retrieve');
    expect(statements).toHaveLength(1);
    expect(resourcesOf(statements[0])).toEqual([
      `arn:aws:bedrock:${REGION}:${ACCOUNT}:knowledge-base/*`,
    ]);
  });

  test('AgentCore Memory の操作のリソースは Memory の ARN と <ARN>/*', () => {
    const statements = withAction(docs, 'bedrock-agentcore:GetMemory');
    expect(statements).toHaveLength(1);
    const memoryArn = exportedRef(`${DATA}/Memory/Default`, 'MemoryArn');
    expect(flat(statements[0]).sort()).toEqual([memoryArn, `${memoryArn}/*`]);
    expect(actionsOf(statements[0])).not.toContain('bedrock-agentcore:ListMemories');
  });

  test('テーブルのインデックスは名前で付け、/index/* はない。対象はアプリが Query する GSI だけ', () => {
    const indexArns = statementsOf(docs)
      .flatMap(flat)
      .filter((r) => r.includes('/index/'));
    expect(indexArns.filter((r) => r.endsWith('/index/*'))).toEqual([]);
    expect([...new Set(indexArns)].sort()).toEqual([...QUERIED_INDEX_ARNS].sort());
  });

  test('インデックスの操作は同じ表への操作と同じで、そのテーブルの ARN の文に並ぶ', () => {
    for (const indexArn of QUERIED_INDEX_ARNS) {
      const table = indexArn.slice(0, indexArn.indexOf('/index/'));
      const statements = statementsOf(docs).filter((s) => flat(s).includes(indexArn));
      expect(statements.length).toBeGreaterThan(0);
      for (const s of statements) {
        expect(flat(s)).toContain(table);
        expect(actionsOf(s)).toContain('dynamodb:Query');
      }
    }
  });

  test('方針の文字数が上限に収まる（Policy 10,240、ManagedPolicy 6,144、ロールごとのインラインの合計 10,240）', () => {
    const size = (d: PolicyDoc) => JSON.stringify(d.document).length;
    const inlineByRole: Record<string, number> = {};
    for (const d of docs) {
      if (d.type === 'AWS::IAM::ManagedPolicy') {
        expect(size(d)).toBeLessThanOrEqual(MAX_MANAGED_POLICY);
        continue;
      }
      expect(size(d)).toBeLessThanOrEqual(MAX_INLINE_POLICY);
      for (const role of d.roles) {
        inlineByRole[role] = (inlineByRole[role] ?? 0) + size(d);
      }
    }
    for (const total of Object.values(inlineByRole)) {
      expect(total).toBeLessThanOrEqual(MAX_INLINE_POLICY);
    }
  });

  test('3 つの秘密を読めるのは実行ロールだけ（今の付け方を保つ。NFR3.14）', () => {
    const readers = (secretLogicalId: string) =>
      [
        ...new Set(
          docs
            .filter((d) =>
              d.document.Statement.some(
                (s) =>
                  actionsOf(s).includes('secretsmanager:GetSecretValue') &&
                  JSON.stringify(resourcesOf(s)).includes(secretLogicalId),
              ),
            )
            .flatMap((d) => d.roles),
        ),
      ].sort();
    for (const secret of ['TokenEncryptionKeys', 'LoginCookieSigningKey', 'CognitoClientSecret']) {
      const roles = readers(secret);
      expect(roles).toHaveLength(1);
      expect(roles[0]).toMatch(/^AppTaskDefinitionExecutionRole[0-9A-F]{8}$/);
    }
    // 秘密を読む文はほかにない（ワイルドカードでも読ませない）
    const all = withAction(docs, 'secretsmanager:GetSecretValue');
    expect(all.flatMap(resourcesOf)).not.toContain('*');
  });
});

describe('IAM: Bedrock Mantle（enableAdditionalPersonaModels: true）', () => {
  const docs = policyDocs(templates({ enableAdditionalPersonaModels: true }));

  test('bedrock-mantle:CreateInference はこのアカウントの project の ARN（リージョンは *）だけ', () => {
    const statements = withAction(docs, 'bedrock-mantle:CreateInference');
    expect(statements).toHaveLength(1);
    expect(resourcesOf(statements[0])).toEqual([
      `arn:aws:bedrock-mantle:*:${ACCOUNT}:project/*`,
    ]);
  });

  test('既定の dev の値では Mantle の操作を付けない', () => {
    const defaults = policyDocs(templates({}));
    const actions = statementsOf(defaults).flatMap(actionsOf);
    expect(actions.filter((a) => a.startsWith('bedrock-mantle:'))).toEqual([]);
  });
});
