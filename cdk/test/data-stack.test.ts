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
  // Personas（U4 で OwnerIndex を足した。既存の 3 つは今と同じ）
  { pk: 'id', gsis: ['CreatedAtIndex', 'NameIndex', 'OccupationIndex', 'OwnerIndex'], pitr: true },
  { pk: 'id', gsis: ['CreatedAtIndex', 'TopicIndex', 'ModeIndex'], pitr: true }, // Discussions
  // UploadedFiles（U5 で TTL expires_at を足した。キー・GSI・PITR は今のまま）
  { pk: 'id', gsis: ['UploadedAtIndex'], pitr: true, ttl: 'expires_at' },
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

describe('DataStack: ResourceShares テーブルと OwnerIndex（U4、NFR1.20、NFR1.21、NFR2.1）', () => {
  function tables(): any[] {
    return Object.values(template.findResources('AWS::DynamoDB::GlobalTable'));
  }

  test('ResourceShares は PK group_id・SK share_key、PITR、RETAIN、削除保護、GSI なし', () => {
    const shares = tables().find((t) => t.Properties.KeySchema[0].AttributeName === 'group_id');
    expect(shares).toBeDefined();
    expect(shares.DeletionPolicy).toBe('Retain');
    expect(shares.UpdateReplacePolicy).toBe('Retain');
    expect(shares.Properties.KeySchema).toEqual([
      { AttributeName: 'group_id', KeyType: 'HASH' },
      { AttributeName: 'share_key', KeyType: 'RANGE' },
    ]);
    expect(shares.Properties.BillingMode).toBe('PAY_PER_REQUEST');
    expect(shares.Properties.GlobalSecondaryIndexes).toBeUndefined();
    expect(shares.Properties.TimeToLiveSpecification).toBeUndefined();
    const replica = shares.Properties.Replicas[0];
    expect(replica.PointInTimeRecoverySpecification).toEqual({ PointInTimeRecoveryEnabled: true });
    expect(replica.DeletionProtectionEnabled).toBe(true);
  });

  test('Personas の OwnerIndex は PK owner_sub・SK created_at、キーだけの射影', () => {
    const personas = tables().find((t) =>
      (t.Properties.GlobalSecondaryIndexes ?? []).some((g: any) => g.IndexName === 'OwnerIndex'),
    );
    expect(personas).toBeDefined();
    const indexes = Object.fromEntries(
      personas.Properties.GlobalSecondaryIndexes.map((g: any) => [g.IndexName, g]),
    );
    expect(indexes.OwnerIndex.KeySchema).toEqual([
      { AttributeName: 'owner_sub', KeyType: 'HASH' },
      { AttributeName: 'created_at', KeyType: 'RANGE' },
    ]);
    expect(indexes.OwnerIndex.Projection).toEqual({ ProjectionType: 'KEYS_ONLY' });
    // 既存の GSI の射影は今と同じ（すべての列）
    for (const name of ['CreatedAtIndex', 'NameIndex', 'OccupationIndex']) {
      expect(indexes[name].Projection).toEqual({ ProjectionType: 'ALL' });
    }
  });
});

describe('DataStack: PersonaCandidates と PersonaBindings（U5、NFR1.40、NFR9.30）', () => {
  function byKeys(...names: string[]): any {
    return Object.values(template.findResources('AWS::DynamoDB::GlobalTable')).find(
      (t) => JSON.stringify(t.Properties.KeySchema.map((k: any) => k.AttributeName)) ===
        JSON.stringify(names),
    );
  }

  test('PersonaCandidates は PK candidate_id、TTL expires_at、PITR なし、RETAIN、削除保護なし、GSI なし', () => {
    const candidates = byKeys('candidate_id');
    expect(candidates).toBeDefined();
    expect(candidates.DeletionPolicy).toBe('Retain');
    expect(candidates.UpdateReplacePolicy).toBe('Retain');
    expect(candidates.Properties.BillingMode).toBe('PAY_PER_REQUEST');
    expect(candidates.Properties.TimeToLiveSpecification).toEqual({
      AttributeName: 'expires_at',
      Enabled: true,
    });
    expect(candidates.Properties.GlobalSecondaryIndexes).toBeUndefined();
    const replica = candidates.Properties.Replicas[0];
    expect(replica.PointInTimeRecoverySpecification).toBeUndefined();
    // 8 時間の一時データで、作り直しても失うものがない（Infrastructure Design R 2 回目）
    expect(replica.DeletionProtectionEnabled).toBeUndefined();
  });

  test('PersonaBindings は PK persona_id・SK target_key、GSI TargetIndex（KEYS_ONLY）、PITR、RETAIN、削除保護', () => {
    const bindings = byKeys('persona_id', 'target_key');
    expect(bindings).toBeDefined();
    expect(bindings.DeletionPolicy).toBe('Retain');
    expect(bindings.UpdateReplacePolicy).toBe('Retain');
    expect(bindings.Properties.KeySchema).toEqual([
      { AttributeName: 'persona_id', KeyType: 'HASH' },
      { AttributeName: 'target_key', KeyType: 'RANGE' },
    ]);
    expect(bindings.Properties.GlobalSecondaryIndexes).toEqual([
      {
        IndexName: 'TargetIndex',
        KeySchema: [{ AttributeName: 'target_id', KeyType: 'HASH' }],
        Projection: { ProjectionType: 'KEYS_ONLY' },
      },
    ]);
    expect(bindings.Properties.TimeToLiveSpecification).toBeUndefined();
    const replica = bindings.Properties.Replicas[0];
    expect(replica.PointInTimeRecoverySpecification).toEqual({ PointInTimeRecoveryEnabled: true });
    expect(replica.DeletionProtectionEnabled).toBe(true);
  });
});

describe('DataStack: アップロード用バケットのライフサイクル（U5、NFR1.33、NFR1.41）', () => {
  function rules(): any[] {
    const bucket = Object.values(template.findResources('AWS::S3::Bucket'))[0];
    return bucket.Properties.LifecycleConfiguration?.Rules ?? [];
  }

  test('ルールは 3 つで、どれも有効', () => {
    expect(rules()).toHaveLength(3);
    for (const rule of rules()) {
      expect(rule.Status).toBe('Enabled');
    }
  });

  test('candidates/ の一時 CSV は 1 日で失効', () => {
    const rule = rules().find((r) => r.Id === 'ExpireCandidateCsv');
    expect(rule).toMatchObject({ Prefix: 'candidates/', ExpirationInDays: 1 });
  });

  test('タグ pending=true の紐付かなかったアップロードは 1 日で失効', () => {
    const rule = rules().find((r) => r.Id === 'ExpireUnattachedUploads');
    expect(rule.TagFilters).toEqual([{ Key: 'pending', Value: 'true' }]);
    expect(rule.ExpirationInDays).toBe(1);
    expect(rule.Prefix).toBeUndefined();
  });

  test('古いバージョンは 7 日、期限切れの削除マーカーを掃除、未完了のマルチパートは 7 日', () => {
    const rule = rules().find((r) => r.Id === 'ExpireNoncurrentVersions');
    expect(rule.NoncurrentVersionExpiration).toEqual({ NoncurrentDays: 7 });
    expect(rule.ExpiredObjectDeleteMarker).toBe(true);
    expect(rule.AbortIncompleteMultipartUpload).toEqual({ DaysAfterInitiation: 7 });
    expect(rule.Prefix).toBeUndefined();
    expect(rule.TagFilters).toBeUndefined();
  });

  test('バージョニング・暗号化・公開アクセスの遮断は今のまま', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      VersioningConfiguration: { Status: 'Enabled' },
    });
  });
});

describe('DataStack: 既存の 10 テーブル', () => {
  test('Sessions・ResourceShares・U5 の 2 つを含めて 14 テーブル。物理名はなく、すべて RETAIN', () => {
    const tables = template.findResources('AWS::DynamoDB::GlobalTable');
    expect(Object.keys(tables)).toHaveLength(14);
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
