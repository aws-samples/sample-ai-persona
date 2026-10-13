import { Construct } from 'constructs';
import { RemovalPolicy } from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';

/**
 * v1.0.0 の DynamoDB テーブル（Data スタック）。
 *
 * - 今と同じキー・GSI・PITR の 10 テーブルを TableV2 で新しく作る（形の作り直しは U4〜U7）
 * - ログインセッションの Sessions テーブルを足す（カスタマー管理の KMS キー、TTL、PITR なし）
 * - 物理名は自動生成にし、App スタックへは参照で渡す（約束 C10）
 * - すべて RETAIN と削除保護（NFR9.10、project.md の Forbidden）
 */
export class Database extends Construct {
  public readonly personasTable: dynamodb.TableV2;
  public readonly discussionsTable: dynamodb.TableV2;
  public readonly uploadedFilesTable: dynamodb.TableV2;
  public readonly datasetsTable: dynamodb.TableV2;
  public readonly bindingsTable: dynamodb.TableV2;
  public readonly surveyTemplatesTable: dynamodb.TableV2;
  public readonly surveysTable: dynamodb.TableV2;
  public readonly knowledgeBasesTable: dynamodb.TableV2;
  public readonly personaKBBindingsTable: dynamodb.TableV2;
  public readonly jobsTable: dynamodb.TableV2;
  public readonly sessionsTable: dynamodb.TableV2;
  public readonly sessionsKey: kms.Key;
  /**
   * 部署ごとの共有の索引（U4、ADR-003）。applicationTables には入れない: そちらの
   * 一括付与（grantReadWriteData）は Scan を含むため、App スタックが 7 つの操作だけを付ける。
   */
  public readonly resourceSharesTable: dynamodb.TableV2;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    const gsi = (
      indexName: string,
      partitionKey: string,
      sortKey?: string,
      projectionType?: dynamodb.ProjectionType,
    ): dynamodb.GlobalSecondaryIndexPropsV2 => ({
      indexName,
      partitionKey: { name: partitionKey, type: dynamodb.AttributeType.STRING },
      ...(sortKey ? { sortKey: { name: sortKey, type: dynamodb.AttributeType.STRING } } : {}),
      ...(projectionType ? { projectionType } : {}),
    });

    this.personasTable = this.table('Personas', {
      globalSecondaryIndexes: [
        gsi('CreatedAtIndex', 'type', 'created_at'),
        gsi('NameIndex', 'type', 'name'),
        gsi('OccupationIndex', 'type', 'occupation'),
        // 「自分のもの」の一覧と件数（U4、BR3.2）。owner_sub のない項目は入らない
        gsi('OwnerIndex', 'owner_sub', 'created_at', dynamodb.ProjectionType.KEYS_ONLY),
      ],
    });
    this.discussionsTable = this.table('Discussions', {
      globalSecondaryIndexes: [
        gsi('CreatedAtIndex', 'type', 'created_at'),
        gsi('TopicIndex', 'type', 'topic'),
        gsi('ModeIndex', 'mode', 'created_at'),
      ],
    });
    this.uploadedFilesTable = this.table('UploadedFiles', {
      globalSecondaryIndexes: [gsi('UploadedAtIndex', 'type', 'uploaded_at')],
    });
    this.datasetsTable = this.table('Datasets');
    this.bindingsTable = this.table('PersonaDatasetBindings', {
      globalSecondaryIndexes: [gsi('PersonaIdIndex', 'persona_id')],
    });
    this.surveyTemplatesTable = this.table('SurveyTemplates');
    this.surveysTable = this.table('Surveys');
    this.knowledgeBasesTable = this.table('KnowledgeBases');
    this.personaKBBindingsTable = this.table('PersonaKBBindings', {
      globalSecondaryIndexes: [gsi('PersonaIdIndex', 'persona_id')],
    });
    // Jobs: 非同期ジョブの追跡。今と同じく PITR なし、TTL は expires_at
    this.jobsTable = this.table('Jobs', { pitr: false, timeToLiveAttribute: 'expires_at' });

    // Sessions: ログインセッション（NFR3.4、reliability-design.md）。
    // 業務データを持たず、消えても再ログインで済むため PITR は付けない。
    this.sessionsKey = new kms.Key(this, 'SessionsKey', {
      description: 'AI Persona v1 Sessions table encryption',
      enableKeyRotation: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    this.sessionsTable = new dynamodb.TableV2(this, 'Sessions', {
      partitionKey: { name: 'session_id_hash', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.customerManagedKey(this.sessionsKey),
      timeToLiveAttribute: 'ttl',
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // ResourceShares: PK group_id、SK share_key（{resource_type}#{created_at}#{resource_id}、約束 C8）。
    // 本体から作り直せる索引だが、作り直しの手間を避けるため PITR と RETAIN を付ける
    this.resourceSharesTable = new dynamodb.TableV2(this, 'ResourceShares', {
      partitionKey: { name: 'group_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'share_key', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
  }

  private table(
    id: string,
    options: {
      globalSecondaryIndexes?: dynamodb.GlobalSecondaryIndexPropsV2[];
      pitr?: boolean;
      timeToLiveAttribute?: string;
    } = {},
  ): dynamodb.TableV2 {
    const { globalSecondaryIndexes, pitr = true, timeToLiveAttribute } = options;
    return new dynamodb.TableV2(this, id, {
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      globalSecondaryIndexes,
      timeToLiveAttribute,
      ...(pitr
        ? { pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true } }
        : {}),
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
  }

  /** App スタックがタスクロールに権限を与え、環境変数に名前を渡す 10 テーブル。 */
  public get applicationTables(): Record<string, dynamodb.TableV2> {
    return {
      PERSONAS_TABLE_NAME: this.personasTable,
      DISCUSSIONS_TABLE_NAME: this.discussionsTable,
      UPLOADED_FILES_TABLE_NAME: this.uploadedFilesTable,
      DATASETS_TABLE_NAME: this.datasetsTable,
      PERSONA_DATASET_BINDINGS_TABLE_NAME: this.bindingsTable,
      SURVEY_TEMPLATES_TABLE_NAME: this.surveyTemplatesTable,
      SURVEYS_TABLE_NAME: this.surveysTable,
      KNOWLEDGE_BASES_TABLE_NAME: this.knowledgeBasesTable,
      PERSONA_KB_BINDINGS_TABLE_NAME: this.personaKBBindingsTable,
      JOBS_TABLE_NAME: this.jobsTable,
    };
  }
}
