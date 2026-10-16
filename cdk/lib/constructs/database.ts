import { Construct } from 'constructs';
import { RemovalPolicy } from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';

/**
 * v1.0.0 の DynamoDB テーブル（Data スタック）。
 *
 * - 今と同じキー・GSI・PITR の 10 テーブルを TableV2 で新しく作る（形の作り直しは U4〜U7）
 * - U5: 生成候補（PersonaCandidates）と紐付け（PersonaBindings）を足し、UploadedFiles に TTL を足す
 * - U6: 議論の発言・レポートとインタビューの 4 テーブルを足し、Discussions に OwnerIndex を足す
 * - U7: カスタムペルソナデータの登録情報（CustomPersonaData）を足し、SurveyTemplates・Surveys に
 *   OwnerIndex を足す
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
  /**
   * 生成候補（U5、BR1.1）。8 時間の一時データで、TTL expires_at で消える。PITR と削除保護は
   * 付けない（作り直しても失うものがない）。applicationTables には入れず、App スタックが
   * 6 つの操作だけを付ける（Scan・Query なし）。
   */
  public readonly personaCandidatesTable: dynamodb.TableV2;
  /**
   * ペルソナと組織資産の紐付け（U5、ADR-007）。PK persona_id・SK target_key、逆引きの
   * TargetIndex（KEYS_ONLY）。applicationTables には入れず、App スタックが Scan を除く
   * 7 つの操作だけを付ける。
   */
  public readonly personaBindingsTable: dynamodb.TableV2;
  /**
   * 議論の発言（U6、ADR-002）。PK discussion_id・SK seq（数値）。本体の判定を通ったあとにだけ読む。
   * 以下の U6 の 4 テーブルは applicationTables に入れず、App スタックが Scan を除く
   * 8 つの操作を 1 つの文で付ける（タスクロールのポリシーの大きさ、NFR1.64）。
   */
  public readonly discussionMessagesTable: dynamodb.TableV2;
  /** 議論のレポート（U6、ADR-002）。PK discussion_id・SK report_id。 */
  public readonly discussionReportsTable: dynamodb.TableV2;
  /**
   * 実行中のインタビュー（U6、ADR-004）。最長 7 日の一時データで TTL expires_at で消える。
   * PITR と削除保護は付けない（PersonaCandidates と同じ扱い）。
   */
  public readonly interviewSessionsTable: dynamodb.TableV2;
  /** 実行中のインタビューの発言（U6）。PK session_id・SK seq（数値）、TTL expires_at。 */
  public readonly interviewMessagesTable: dynamodb.TableV2;
  /**
   * カスタムペルソナデータの登録情報（U7、BR4.3〜BR4.6）。PK id、GSI なし、PITR・削除保護・
   * RETAIN・AWS 管理キー。applicationTables には入れず、App スタックが Query を除く
   * 7 つの操作を 1 つの文で付ける（タスクロールのポリシーの大きさ、NFR1.81）。
   */
  public readonly customPersonaDataTable: dynamodb.TableV2;

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
        // 「自分のもの」の一覧と件数（U6、BR2.1）。テーブルは置き換えない（NFR1.65）。
        // 型固定の 3 つは U9 で外す
        gsi('OwnerIndex', 'owner_sub', 'created_at', dynamodb.ProjectionType.KEYS_ONLY),
      ],
    });
    // U5: 紐付かなかったアップロードのメタデータは 24 時間後に TTL で消える（BR5.6）。
    // TTL の設定はテーブルの属性の更新で、テーブルは置き換わらない
    this.uploadedFilesTable = this.table('UploadedFiles', {
      globalSecondaryIndexes: [gsi('UploadedAtIndex', 'type', 'uploaded_at')],
      timeToLiveAttribute: 'expires_at',
    });
    this.datasetsTable = this.table('Datasets');
    this.bindingsTable = this.table('PersonaDatasetBindings', {
      globalSecondaryIndexes: [gsi('PersonaIdIndex', 'persona_id')],
    });
    // 「自分のもの」の一覧と件数（U7、BR1.2）。テーブルは置き換えない（GSI の追加だけ）
    this.surveyTemplatesTable = this.table('SurveyTemplates', {
      globalSecondaryIndexes: [
        gsi('OwnerIndex', 'owner_sub', 'created_at', dynamodb.ProjectionType.KEYS_ONLY),
      ],
    });
    this.surveysTable = this.table('Surveys', {
      globalSecondaryIndexes: [
        gsi('OwnerIndex', 'owner_sub', 'created_at', dynamodb.ProjectionType.KEYS_ONLY),
      ],
    });
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

    // PersonaCandidates（U5、NFR1.40）: table() は PK を id に固定しているため直接作る
    this.personaCandidatesTable = new dynamodb.TableV2(this, 'PersonaCandidates', {
      partitionKey: { name: 'candidate_id', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      timeToLiveAttribute: 'expires_at',
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // PersonaBindings（U5、ADR-007）: 紐付けは消えると戻せないため PITR と削除保護を付ける
    this.personaBindingsTable = new dynamodb.TableV2(this, 'PersonaBindings', {
      partitionKey: { name: 'persona_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'target_key', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      globalSecondaryIndexes: [
        gsi('TargetIndex', 'target_id', undefined, dynamodb.ProjectionType.KEYS_ONLY),
      ],
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // U7: カスタムペルソナデータの登録情報（PK id、名前の押さえの項目 id="name#{名前}" も同じ
    // テーブル、約束 C8）。applicationTables には入れず、App スタックが絞った操作を付ける
    this.customPersonaDataTable = this.table('CustomPersonaData');

    // U6: 議論の発言とレポート（ADR-002）。消えると戻せないため PITR と削除保護を付ける
    const seq = { name: 'seq', type: dynamodb.AttributeType.NUMBER };
    this.discussionMessagesTable = this.childTable('DiscussionMessages', 'discussion_id', seq);
    this.discussionReportsTable = this.childTable('DiscussionReports', 'discussion_id', {
      name: 'report_id',
      type: dynamodb.AttributeType.STRING,
    });

    // U6: 実行中のインタビュー（ADR-004）。最長 7 日の一時データ
    this.interviewSessionsTable = new dynamodb.TableV2(this, 'InterviewSessions', {
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      timeToLiveAttribute: 'expires_at',
      removalPolicy: RemovalPolicy.RETAIN,
    });
    // TTL はセッションの最長より長い（created_at + 8 日）
    this.interviewMessagesTable = new dynamodb.TableV2(this, 'InterviewMessages', {
      partitionKey: { name: 'session_id', type: dynamodb.AttributeType.STRING },
      sortKey: seq,
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      timeToLiveAttribute: 'expires_at',
      removalPolicy: RemovalPolicy.RETAIN,
    });
  }

  /** A table keyed by its parent's ID and a sort key, with PITR and deletion protection. */
  private childTable(
    id: string,
    partitionKey: string,
    sortKey: dynamodb.Attribute,
  ): dynamodb.TableV2 {
    return new dynamodb.TableV2(this, id, {
      partitionKey: { name: partitionKey, type: dynamodb.AttributeType.STRING },
      sortKey,
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
  }

  /** U6 の 4 テーブル（App スタックが 1 つの文で絞った操作を付ける）。 */
  public get discussionChildTables(): Record<string, dynamodb.TableV2> {
    return {
      DISCUSSION_MESSAGES_TABLE_NAME: this.discussionMessagesTable,
      DISCUSSION_REPORTS_TABLE_NAME: this.discussionReportsTable,
      INTERVIEW_SESSIONS_TABLE_NAME: this.interviewSessionsTable,
      INTERVIEW_MESSAGES_TABLE_NAME: this.interviewMessagesTable,
    };
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

  /**
   * App スタックがタスクロールに権限を与え、環境変数に名前を渡す 8 テーブル。
   * U5: 古い紐付けの 2 テーブル（PersonaDatasetBindings・PersonaKBBindings）は外した
   * （RETAIN のまま残し、テーブルを外すのは U9）。
   */
  public get applicationTables(): Record<string, dynamodb.TableV2> {
    return {
      PERSONAS_TABLE_NAME: this.personasTable,
      DISCUSSIONS_TABLE_NAME: this.discussionsTable,
      UPLOADED_FILES_TABLE_NAME: this.uploadedFilesTable,
      DATASETS_TABLE_NAME: this.datasetsTable,
      SURVEY_TEMPLATES_TABLE_NAME: this.surveyTemplatesTable,
      SURVEYS_TABLE_NAME: this.surveysTable,
      KNOWLEDGE_BASES_TABLE_NAME: this.knowledgeBasesTable,
      JOBS_TABLE_NAME: this.jobsTable,
    };
  }
}
