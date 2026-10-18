import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { createDevStage } from '../lib/dev-stage';
import { PATH_METADATA_CONTEXT, pathOf } from './template-paths';

/**
 * データを持つリソースの construct のパスと論理 ID・RETAIN・削除保護（U9、NFR7.17、NFR7.18、約束 C10）。
 *
 * 論理 ID が変わると CloudFormation は新しいリソースを作り、アプリは空のテーブルへ
 * 切り替わる。CDK は論理 ID を construct のパスから決めるため、(1) 種類ごとのリソースの
 * パスの集合が期待の一覧と過不足なく一致すること、(2) 各リソースの論理 ID が CDK がその
 * パスに既定で割り当てる値と同じこと（overrideLogicalId・renameLogicalId を使っていない）
 * の 2 つで論理 ID を固定する。テーブルや秘密を足す変更は、同じ変更でこの一覧
 * （EXPECTED_PATHS）を直す。construct ID を変える・construct を別の親へ移す変更は
 * (1) で失敗する。
 */

jest.setTimeout(60_000);

const DATA = 'AIPersonaDev/Data';
const table = (id: string) => `${DATA}/Database/${id}/Resource`;

/** Data スタックの種類ごとの期待の construct のパス。テーブルを足す Unit はここに足す（C10）。 */
const EXPECTED_PATHS: Record<string, string[]> = {
  'AWS::DynamoDB::GlobalTable': [
    'CustomPersonaData',
    'Datasets',
    'DiscussionMessages',
    'DiscussionReports',
    'Discussions',
    'InterviewMessages',
    'InterviewSessions',
    'Jobs',
    'KnowledgeBases',
    'PersonaBindings',
    'PersonaCandidates',
    'Personas',
    'ResourceShares',
    'Sessions',
    'SurveyTemplates',
    'Surveys',
    'UploadedFiles',
    // PersonaDatasetBindings・PersonaKBBindings は U9 で CDK の管理から外した（NFR2.28。RETAIN で残る）
  ].map(table),
  'AWS::KMS::Key': [`${DATA}/Database/SessionsKey/Resource`],
  'AWS::S3::Bucket': [`${DATA}/UploadBucket/Bucket/Resource`],
  'AWS::Cognito::UserPool': [`${DATA}/Cognito/UserPool/Resource`],
  'AWS::ECR::Repository': [`${DATA}/Ecr/Repository/Resource`],
  'AWS::BedrockAgentCore::Memory': [`${DATA}/Memory/Default`],
  'AWS::SecretsManager::Secret': [
    `${DATA}/LoginCookieSigningKey/Resource`,
    `${DATA}/TokenEncryptionKeys/Resource`,
  ],
};

/**
 * 削除保護を付けないテーブル（U5・U6 の決定）。TTL で消える一時データで、作り直しても
 * 失うものがないため。RETAIN は付いている。ここにないテーブルは削除保護が要る。
 */
const TABLES_WITHOUT_DELETION_PROTECTION = [
  table('InterviewMessages'), // 実行中のインタビューの発言（U6、最長 8 日）
  table('InterviewSessions'), // 実行中のインタビュー（U6、最長 7 日）
  table('PersonaCandidates'), // 生成候補（U5、8 時間）
];

const STATEFUL_TYPES = Object.keys(EXPECTED_PATHS);

// eslint-disable-next-line @typescript-eslint/no-require-imports
const FEATURE_FLAGS: Record<string, unknown> = require('../cdk.json').context;

const stage = createDevStage(
  new cdk.App({ context: { ...FEATURE_FLAGS, ...PATH_METADATA_CONTEXT } }),
  { account: '123456789012' },
);
const data = Template.fromStack(stage.data);
const app = Template.fromStack(stage.app);

/** Template resources of one type, keyed by construct path (with their logical IDs). */
function byPath(type: string): Map<string, { logicalId: string; resource: any }> {
  const found = new Map<string, { logicalId: string; resource: any }>();
  for (const [logicalId, resource] of Object.entries(data.findResources(type))) {
    const path = pathOf(resource);
    expect(found.has(path)).toBe(false);
    found.set(path, { logicalId, resource });
  }
  return found;
}

/** The CfnElement at a construct path inside the Data stack. */
function elementAt(path: string): cdk.CfnElement {
  const element = stage.data.node.findAll().find((c) => c.node.path === path);
  if (!element || !cdk.CfnElement.isCfnElement(element)) {
    throw new Error(`No CfnElement at ${path}`);
  }
  return element;
}

/** The logical ID CDK allocates for an element by default (its path, no rename or override). */
function defaultLogicalId(element: cdk.CfnElement): string {
  // allocateLogicalId は Stack の protected の API（論理 ID の既定の割り当て方）
  const stack = cdk.Stack.of(element) as unknown as {
    allocateLogicalId(e: cdk.CfnElement): string;
  };
  return stack.allocateLogicalId(element);
}

test.each(STATEFUL_TYPES)('%s の construct のパスが期待の一覧と過不足なく一致する（NFR7.17）', (type) => {
  expect([...byPath(type).keys()].sort()).toEqual([...EXPECTED_PATHS[type]].sort());
});

test.each(STATEFUL_TYPES)(
  '%s の論理 ID は CDK がそのパスに既定で割り当てる値と同じ（上書き・付け替えなし、NFR7.17）',
  (type) => {
    const resources = byPath(type);
    expect(resources.size).toBeGreaterThan(0);
    for (const [path, { logicalId }] of resources) {
      const element = elementAt(path);
      // getLogicalId は renameLogicalId を反映し、overrideLogicalId は反映しない
      expect(logicalId).toBe(stage.data.getLogicalId(element));
      expect(logicalId).toBe(defaultLogicalId(element));
    }
  },
);

test.each(STATEFUL_TYPES)('%s は 1 件以上あり、全件が DeletionPolicy・UpdateReplacePolicy とも Retain（NFR7.18）', (type) => {
  const resources = Object.values(data.findResources(type));
  expect(resources.length).toBeGreaterThan(0);
  for (const resource of resources) {
    expect(resource.DeletionPolicy).toBe('Retain');
    expect(resource.UpdateReplacePolicy).toBe('Retain');
  }
});

test('GlobalTable の Replicas の削除保護: 一時データの 3 テーブルを除く全件で true（NFR7.18）', () => {
  const tables = byPath('AWS::DynamoDB::GlobalTable');
  const unprotected = [...tables]
    .filter(([, { resource }]) =>
      (resource.Properties.Replicas as any[]).some((r) => r.DeletionProtectionEnabled !== true),
    )
    .map(([path]) => path)
    .sort();
  expect(unprotected).toEqual([...TABLES_WITHOUT_DELETION_PROTECTION].sort());
  for (const { resource } of tables.values()) {
    expect((resource.Properties.Replicas as any[]).length).toBeGreaterThan(0);
  }
});

test('User Pool は DeletionProtection: ACTIVE（NFR7.18）', () => {
  const pools = Object.values(data.findResources('AWS::Cognito::UserPool'));
  expect(pools.length).toBeGreaterThan(0);
  for (const pool of pools) {
    expect(pool.Properties.DeletionProtection).toBe('ACTIVE');
  }
});

test.each(['AWS::SecretsManager::Secret', 'AWS::Logs::LogGroup'])(
  'App スタックの %s は 1 件以上あり、全件が RETAIN（NFR3.14、NFR8.4）',
  (type) => {
    const resources = Object.values(app.findResources(type));
    expect(resources.length).toBeGreaterThan(0);
    for (const resource of resources) {
      expect(resource.DeletionPolicy).toBe('Retain');
      expect(resource.UpdateReplacePolicy).toBe('Retain');
    }
  },
);
