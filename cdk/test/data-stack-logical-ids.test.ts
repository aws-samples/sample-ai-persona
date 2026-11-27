import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AIPersonaStage } from '../lib/ai-persona-stage';
import { createClosedDevStage, createDevStage } from '../lib/dev-stage';
import { ACCOUNT, closedContext, newApp } from './helpers/stages';
import { pathOf } from './template-paths';

/**
 * Construct paths, logical IDs, RETAIN and deletion protection of resources that hold data.
 *
 * When a logical ID changes, CloudFormation creates a new resource and the app switches
 * to an empty table. CDK derives logical IDs from construct paths, so they are pinned by
 * two checks: (1) the set of resource paths per kind matches the expected list exactly,
 * and (2) each resource's logical ID equals the value CDK assigns to that path by default
 * (overrideLogicalId and renameLogicalId are not used). A change that adds a table or secret
 * must update this list (expectedPaths) in the same change. Changing a construct ID or moving
 * a construct to another parent fails (1).
 *
 * Applied to both the public Stage AIPersonaDev and the closed Stage AIPersonaDevClosed
 * (the closed Stage uses the same definitions and protection. Its User Pool has no domain,
 * but a domain holds no data, so it is not in this list).
 */

jest.setTimeout(60_000);

/** Expected construct paths per kind in the Data stack. Add new tables here. */
function expectedPaths(data: string): Record<string, string[]> {
  const table = (id: string) => `${data}/Database/${id}/Resource`;
  return {
    'AWS::DynamoDB::GlobalTable': [
      // Advanced generation (beta)
      'AdvancedGenerations',
      'AppSettings',
      'SourceFiles',
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
      // Records of the facilitator's assignee (moved to Step Functions)
      'SurveyRunLease',
      'SurveyTemplates',
      'Surveys',
      'UploadedFiles',
      // PersonaDatasetBindings and PersonaKBBindings are no longer managed by CDK (kept by RETAIN)
    ].map(table),
    'AWS::KMS::Key': [`${data}/Database/SessionsKey/Resource`],
    'AWS::S3::Bucket': [`${data}/UploadBucket/Bucket/Resource`],
    'AWS::Cognito::UserPool': [`${data}/Cognito/UserPool/Resource`],
    'AWS::ECR::Repository': [`${data}/Ecr/Repository/Resource`],
    'AWS::BedrockAgentCore::Memory': [`${data}/Memory/Default`],
    'AWS::SecretsManager::Secret': [
      `${data}/LoginCookieSigningKey/Resource`,
      `${data}/TokenEncryptionKeys/Resource`,
    ],
  };
}

/**
 * Tables without deletion protection. They hold temporary data that expires via TTL and
 * lose nothing when recreated. RETAIN is still set. Tables not listed here need deletion protection.
 */
function tablesWithoutDeletionProtection(data: string): string[] {
  const table = (id: string) => `${data}/Database/${id}/Resource`;
  return [
    table('InterviewMessages'), // 実行中のインタビューの発言（U6、最長 8 日）
    table('InterviewSessions'), // 実行中のインタビュー（U6、最長 7 日）
    table('PersonaCandidates'), // 生成候補（U5、8 時間）
    // Admin settings (advanced generation): PITR and RETAIN. Holds no personal data
    table('AppSettings'),
  ];
}

const STATEFUL_TYPES = Object.keys(expectedPaths('-'));

interface Variant {
  name: string;
  build: () => AIPersonaStage;
}

const VARIANTS: Variant[] = [
  {
    name: 'AIPersonaDev',
    build: () => createDevStage(newApp(), { account: ACCOUNT }),
  },
  {
    name: 'AIPersonaDevClosed',
    build: () => {
      const stage = createClosedDevStage(newApp(closedContext()), { account: ACCOUNT });
      if (!stage) {
        throw new Error('The closed Stage was not built.');
      }
      return stage;
    },
  },
];

describe.each(VARIANTS)('$name の Data スタック', ({ name, build }) => {
  const DATA = `${name}/Data`;
  const EXPECTED_PATHS = expectedPaths(DATA);
  const TABLES_WITHOUT_DELETION_PROTECTION = tablesWithoutDeletionProtection(DATA);

  const stage = build();
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
    // allocateLogicalId is a protected Stack API (the default way logical IDs are assigned)
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
        // getLogicalId reflects renameLogicalId but not overrideLogicalId
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

  test('GlobalTable の Replicas の削除保護: 一時データの 3 テーブルと AppSettings を除く全件で true（NFR7.18）', () => {
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
});
