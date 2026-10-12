import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { DataStack } from '../lib/data-stack';
import { devParameter } from '../parameters';

/**
 * Data スタック（状態を持つもの）の検査。FR9.1、FR9.2、NFR3.4、NFR9.7、NFR9.10、約束 C10。
 * ステートフルなリソースはすべて RETAIN で、物理名は自動生成にする。
 */

const ENV = { account: '123456789012', region: 'ap-northeast-1' };

function synth(): Template {
  const app = new cdk.App();
  const stack = new DataStack(app, 'Data', { env: ENV, parameter: devParameter });
  return Template.fromStack(stack);
}

const template = synth();

const EXISTING_TABLES: { pk: string; gsis: string[]; pitr: boolean; ttl?: string }[] = [
  { pk: 'id', gsis: ['CreatedAtIndex', 'NameIndex', 'OccupationIndex'], pitr: true }, // Personas
  { pk: 'id', gsis: ['CreatedAtIndex', 'TopicIndex', 'ModeIndex'], pitr: true }, // Discussions
  { pk: 'id', gsis: ['UploadedAtIndex'], pitr: true }, // UploadedFiles
  { pk: 'id', gsis: ['PersonaIdIndex'], pitr: true }, // PersonaDatasetBindings
  { pk: 'id', gsis: ['PersonaIdIndex'], pitr: true }, // PersonaKBBindings
  { pk: 'id', gsis: [], pitr: false, ttl: 'expires_at' }, // Jobs
];

describe('DataStack: Sessions テーブル', () => {
  test('PK・TTL・オンデマンド・カスタマー管理キー・PITR なし・RETAIN', () => {
    template.hasResource('AWS::DynamoDB::GlobalTable', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
      Properties: Match.objectLike({
        KeySchema: [{ AttributeName: 'session_id_hash', KeyType: 'HASH' }],
        BillingMode: 'PAY_PER_REQUEST',
        TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
        SSESpecification: Match.objectLike({ SSEEnabled: true, SSEType: 'KMS' }),
        Replicas: [
          Match.objectLike({
            Region: 'ap-northeast-1',
            SSESpecification: { KMSMasterKeyId: Match.anyValue() },
            PointInTimeRecoverySpecification: Match.absent(),
          }),
        ],
      }),
    });
  });

  test('KMS キーは自動ローテーションありで RETAIN', () => {
    template.hasResource('AWS::KMS::Key', {
      DeletionPolicy: 'Retain',
      Properties: Match.objectLike({ EnableKeyRotation: true }),
    });
  });
});

describe('DataStack: 既存の 10 テーブル', () => {
  test('Sessions を含めて 11 テーブル。物理名はなく、すべて RETAIN', () => {
    const tables = template.findResources('AWS::DynamoDB::GlobalTable');
    expect(Object.keys(tables)).toHaveLength(11);
    for (const table of Object.values(tables)) {
      expect(table.DeletionPolicy).toBe('Retain');
      expect(table.Properties.TableName).toBeUndefined();
    }
  });

  test.each(EXISTING_TABLES)('キーと GSI と PITR が今と同じ（GSI: $gsis）', ({ pk, gsis, pitr, ttl }) => {
    const tables = Object.values(template.findResources('AWS::DynamoDB::GlobalTable'));
    const match = tables.find((t) => {
      const names = (t.Properties.GlobalSecondaryIndexes ?? []).map((g: any) => g.IndexName).sort();
      const sameTtl = (t.Properties.TimeToLiveSpecification?.AttributeName ?? undefined) === ttl;
      return t.Properties.KeySchema[0].AttributeName === pk
        && JSON.stringify(names) === JSON.stringify([...gsis].sort())
        && sameTtl;
    });
    expect(match).toBeDefined();
    const replica = match!.Properties.Replicas[0];
    expect(Boolean(replica.PointInTimeRecoverySpecification?.PointInTimeRecoveryEnabled)).toBe(pitr);
  });
});

describe('DataStack: S3・ECR・AgentCore Memory', () => {
  test('S3 は暗号化・パブリックアクセス遮断・RETAIN・物理名なし・自動削除なし', () => {
    template.hasResource('AWS::S3::Bucket', {
      DeletionPolicy: 'Retain',
      Properties: Match.objectLike({
        BucketName: Match.absent(),
        BucketEncryption: Match.anyValue(),
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
      }),
    });
    template.resourceCountIs('Custom::S3AutoDeleteObjects', 0);
  });

  test('ECR はタグ不変・最新 20 個を残す・RETAIN', () => {
    template.hasResource('AWS::ECR::Repository', {
      DeletionPolicy: 'Retain',
      Properties: Match.objectLike({
        ImageTagMutability: 'IMMUTABLE',
        LifecyclePolicy: {
          LifecyclePolicyText: Match.stringLikeRegexp('"countNumber":20'),
        },
      }),
    });
  });

  test('AgentCore Memory は v1 の名前で RETAIN', () => {
    template.hasResource('AWS::BedrockAgentCore::Memory', {
      DeletionPolicy: 'Retain',
      Properties: Match.objectLike({ Name: 'AIPersona_v1_dev_memory' }),
    });
  });
});

describe('DataStack: Cognito', () => {
  test('User Pool は Essentials・セルフサインアップなし・削除保護・RETAIN', () => {
    template.hasResource('AWS::Cognito::UserPool', {
      DeletionPolicy: 'Retain',
      Properties: Match.objectLike({
        UserPoolTier: 'ESSENTIALS',
        AdminCreateUserConfig: Match.objectLike({ AllowAdminCreateUserOnly: true }),
        DeletionProtection: 'ACTIVE',
      }),
    });
  });

  test('グループ role:admin と role:member', () => {
    template.hasResourceProperties('AWS::Cognito::UserPoolGroup', { GroupName: 'role:admin' });
    template.hasResourceProperties('AWS::Cognito::UserPoolGroup', { GroupName: 'role:member' });
  });

  test('ドメインは ai-persona-v1-dev-{アカウント} の新しい Managed Login', () => {
    template.hasResourceProperties('AWS::Cognito::UserPoolDomain', {
      Domain: 'ai-persona-v1-dev-123456789012',
      ManagedLoginVersion: 2,
    });
  });
});

describe('DataStack: 秘密', () => {
  test('トークンの暗号化鍵は {"current":"v1"} に k_v1 を生成、RETAIN', () => {
    template.hasResource('AWS::SecretsManager::Secret', {
      DeletionPolicy: 'Retain',
      Properties: Match.objectLike({
        GenerateSecretString: Match.objectLike({
          SecretStringTemplate: '{"current":"v1"}',
          GenerateStringKey: 'k_v1',
          PasswordLength: 64,
          ExcludePunctuation: true,
        }),
      }),
    });
  });

  test('ログイン途中の Cookie の署名鍵は 64 文字の乱数、RETAIN', () => {
    template.hasResource('AWS::SecretsManager::Secret', {
      DeletionPolicy: 'Retain',
      Properties: Match.objectLike({
        GenerateSecretString: {
          PasswordLength: 64,
          ExcludePunctuation: true,
        },
      }),
    });
  });
});

test('DeletionPolicy: Delete のリソースがない', () => {
  const resources = template.toJSON().Resources as Record<string, any>;
  const deletable = Object.entries(resources)
    .filter(([, r]) => r.DeletionPolicy === 'Delete')
    .map(([id]) => id);
  expect(deletable).toEqual([]);
});
