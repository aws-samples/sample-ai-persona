import { Construct } from 'constructs';
import { RemovalPolicy } from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';

/** GSI for listing and counting "my own" items (queried by src/services/stores/resource_share_store.py). */
export const OWNER_INDEX = 'OwnerIndex';
/** GSI for the reverse lookup of bindings (queried by src/services/stores/persona_binding_store.py). */
export const TARGET_INDEX = 'TargetIndex';
/** Sparse index of waiting and running surveys (queried by src/services/stores/survey_store.py). */
export const RUN_QUEUE_INDEX = 'RunQueueIndex';
/**
 * GSI listing an owner's items for advanced generation (beta) (OWNER_INDEX in
 * src/services/stores/source_file_store.py and advanced_generation_store.py). Named differently
 * from OwnerIndex; projection is ALL.
 */
export const OWNER_CREATED_AT_INDEX = 'owner_sub-created_at';
/**
 * GSIs the app queries by IndexName. The App stack grants the task role access only to GSIs
 * with these names (`<table ARN>/index/<name>`).
 */
const APP_QUERIED_INDEXES: ReadonlySet<string> = new Set([
  OWNER_INDEX,
  TARGET_INDEX,
  RUN_QUEUE_INDEX,
  OWNER_CREATED_AT_INDEX,
]);

/**
 * The DynamoDB tables of v1.0.0 (Data stack), all created as TableV2.
 *
 * - Persona candidates (PersonaCandidates) and bindings (PersonaBindings) are included, and
 *   UploadedFiles has a TTL.
 * - Discussion messages and reports plus the interview tables are included, and Discussions
 *   has an OwnerIndex.
 * - Registration info for custom persona data (CustomPersonaData) is included, and
 *   SurveyTemplates and Surveys have an OwnerIndex.
 * - No type-fixed GSIs and no tables for the old bindings.
 * - The login Sessions table uses a customer-managed KMS key, a TTL and no PITR.
 * - Physical names are auto-generated and passed to the App stack by reference.
 * - Everything is RETAIN with deletion protection (except where noted per table).
 */
export class Database extends Construct {
  public readonly personasTable: dynamodb.TableV2;
  public readonly discussionsTable: dynamodb.TableV2;
  public readonly uploadedFilesTable: dynamodb.TableV2;
  public readonly datasetsTable: dynamodb.TableV2;
  public readonly surveyTemplatesTable: dynamodb.TableV2;
  public readonly surveysTable: dynamodb.TableV2;
  public readonly knowledgeBasesTable: dynamodb.TableV2;
  public readonly jobsTable: dynamodb.TableV2;
  public readonly sessionsTable: dynamodb.TableV2;
  public readonly sessionsKey: kms.Key;
  /**
   * Index of sharing per department. Not in applicationTables: that bulk grant
   * (grantReadWriteData) includes Scan, so the App stack grants only 7 actions.
   */
  public readonly resourceSharesTable: dynamodb.TableV2;
  /**
   * Generated candidates: temporary 8-hour data expired by TTL expires_at. No PITR or deletion
   * protection (nothing is lost if it is recreated). Not in applicationTables; the App stack
   * grants only 6 actions (no Scan or Query).
   */
  public readonly personaCandidatesTable: dynamodb.TableV2;
  /**
   * Bindings between personas and organization assets. PK persona_id, SK target_key, with a
   * reverse-lookup TargetIndex (KEYS_ONLY). Not in applicationTables; the App stack grants only
   * 7 actions, without Scan.
   */
  public readonly personaBindingsTable: dynamodb.TableV2;
  /**
   * Discussion messages. PK discussion_id, SK seq (number). Read only after the parent record's
   * check passes. This and the three tables below are not in applicationTables; the App stack
   * grants 8 actions without Scan in a single statement to keep the task role policy small.
   */
  public readonly discussionMessagesTable: dynamodb.TableV2;
  /** Discussion reports. PK discussion_id, SK report_id. */
  public readonly discussionReportsTable: dynamodb.TableV2;
  /**
   * Running interviews: temporary data of up to 7 days, expired by TTL expires_at. No PITR or
   * deletion protection (same as PersonaCandidates).
   */
  public readonly interviewSessionsTable: dynamodb.TableV2;
  /** Messages of running interviews. PK session_id, SK seq (number), TTL expires_at. */
  public readonly interviewMessagesTable: dynamodb.TableV2;
  /**
   * Registration info for custom persona data. PK id, no GSI, PITR, deletion protection, RETAIN
   * and an AWS-managed key. Not in applicationTables; the App stack grants 7 actions without
   * Query in a single statement to keep the task role policy small.
   */
  public readonly customPersonaDataTable: dynamodb.TableV2;
  /**
   * Record of the survey coordinator's assignee (PK id, a single row). Not in
   * applicationTables; the App stack grants only GetItem, PutItem, UpdateItem and DeleteItem.
   */
  public readonly surveyRunLeaseTable: dynamodb.TableV2;
  /**
   * Advanced generation (beta): records of uploaded files. PK source_file_id, GSI
   * owner_sub-created_at. In applicationTables (the App stack grants the bulk actions).
   */
  public readonly sourceFilesTable: dynamodb.TableV2;
  /**
   * Advanced generation: generations, events, result chunks and locks. PK pk, SK sk, GSI
   * owner_sub-created_at, TTL expires_at.
   */
  public readonly advancedGenerationsTable: dynamodb.TableV2;
  /** Administrator settings (read-only for the app). PK setting_key. Holds no personal data. */
  public readonly appSettingsTable: dynamodb.TableV2;

  /** The GSI names defined on each table (recorded where the table is created). */
  private readonly indexNames = new Map<dynamodb.TableV2, string[]>();

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

    // The type-fixed GSIs are all removed. dev recreates its Stage, so they go in one change.
    // An existing table can drop only one GSI per update, so an existing environment must
    // either be deployed with one GSI removed per table at a time, or have its Stage recreated
    this.personasTable = this.table('Personas', {
      globalSecondaryIndexes: [
        // Listing and counting "my own" items; items without owner_sub are not indexed
        gsi(OWNER_INDEX, 'owner_sub', 'created_at', dynamodb.ProjectionType.KEYS_ONLY),
      ],
    });
    this.discussionsTable = this.table('Discussions', {
      globalSecondaryIndexes: [
        // Listing and counting "my own" items; adding it does not replace the table
        gsi(OWNER_INDEX, 'owner_sub', 'created_at', dynamodb.ProjectionType.KEYS_ONLY),
      ],
    });
    // Metadata of uploads that were never bound expires after 24 hours via TTL.
    // Setting a TTL is an attribute update and does not replace the table
    this.uploadedFilesTable = this.table('UploadedFiles', {
      timeToLiveAttribute: 'expires_at',
    });
    this.datasetsTable = this.table('Datasets');
    // The old PersonaDatasetBindings and PersonaKBBindings tables are no longer managed by CDK.
    // They remain in AWS because of RETAIN. To manage them again, import them with cdk import,
    // giving tableName the current physical name (do not let CDK create new ones)
    // Listing and counting "my own" items; only a GSI is added, so the table is not replaced
    this.surveyTemplatesTable = this.table('SurveyTemplates', {
      globalSecondaryIndexes: [
        gsi(OWNER_INDEX, 'owner_sub', 'created_at', dynamodb.ProjectionType.KEYS_ONLY),
      ],
    });
    this.surveysTable = this.table('Surveys', {
      globalSecondaryIndexes: [
        gsi(OWNER_INDEX, 'owner_sub', 'created_at', dynamodb.ProjectionType.KEYS_ONLY),
        // Sparse index holding only waiting and running items (run_queue exists only while queued
        // or running). Projection is ALL so the coordinator can read start_blocked_since etc. from
        // the index alone. The table is not replaced; one update adds just one GSI
        gsi(RUN_QUEUE_INDEX, 'run_queue', 'queued_at', dynamodb.ProjectionType.ALL),
      ],
    });
    this.knowledgeBasesTable = this.table('KnowledgeBases');
    // Jobs: tracks asynchronous jobs. No PITR; TTL is expires_at
    this.jobsTable = this.table('Jobs', { pitr: false, timeToLiveAttribute: 'expires_at' });

    // Sessions: login sessions.
    // They hold no business data and a lost session only means logging in again, so no PITR.
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

    // ResourceShares: PK group_id, SK share_key ({resource_type}#{created_at}#{resource_id}).
    // The index can be rebuilt from the source records, but PITR and RETAIN avoid that effort
    this.resourceSharesTable = new dynamodb.TableV2(this, 'ResourceShares', {
      partitionKey: { name: 'group_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'share_key', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // PersonaCandidates: created directly because table() fixes the PK to id
    this.personaCandidatesTable = new dynamodb.TableV2(this, 'PersonaCandidates', {
      partitionKey: { name: 'candidate_id', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      timeToLiveAttribute: 'expires_at',
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // PersonaBindings: bindings cannot be restored once lost, so PITR and deletion protection are on
    const bindingIndexes = [
      gsi(TARGET_INDEX, 'target_id', undefined, dynamodb.ProjectionType.KEYS_ONLY),
    ];
    this.personaBindingsTable = new dynamodb.TableV2(this, 'PersonaBindings', {
      partitionKey: { name: 'persona_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'target_key', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      globalSecondaryIndexes: bindingIndexes,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    this.recordIndexes(this.personaBindingsTable, bindingIndexes);

    // Registration info for custom persona data (PK id; the name-reservation item id="name#{name}"
    // lives in the same table). Not in applicationTables; the App stack grants narrowed actions
    this.customPersonaDataTable = this.table('CustomPersonaData');

    // Survey coordinator assignee record. A single row with an expiry that is recreated on the
    // next run if lost, so no PITR. Deletion protection, RETAIN and the AWS-managed key match the others
    this.surveyRunLeaseTable = this.table('SurveyRunLease', { pitr: false });

    // Discussion messages and reports: cannot be restored once lost, so PITR and deletion protection are on
    const seq = { name: 'seq', type: dynamodb.AttributeType.NUMBER };
    this.discussionMessagesTable = this.childTable('DiscussionMessages', 'discussion_id', seq);
    this.discussionReportsTable = this.childTable('DiscussionReports', 'discussion_id', {
      name: 'report_id',
      type: dynamodb.AttributeType.STRING,
    });

    // Running interviews: temporary data of up to 7 days
    this.interviewSessionsTable = new dynamodb.TableV2(this, 'InterviewSessions', {
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      timeToLiveAttribute: 'expires_at',
      removalPolicy: RemovalPolicy.RETAIN,
    });
    // The TTL is longer than the longest session (created_at + 8 days)
    this.interviewMessagesTable = new dynamodb.TableV2(this, 'InterviewMessages', {
      partitionKey: { name: 'session_id', type: dynamodb.AttributeType.STRING },
      sortKey: seq,
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      timeToLiveAttribute: 'expires_at',
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // Advanced generation (beta): on-demand, AWS-managed key, PITR and RETAIN (no custom KMS
    // key). These are new tables, so existing tables are not replaced
    const ownerIndexes = [gsi(OWNER_CREATED_AT_INDEX, 'owner_sub', 'created_at')];
    // Expired records are deleted by TTL (attribute expires_at, epoch seconds). Adding a TTL is
    // a table update, not a replacement (test/data-stack-logical-ids.test.ts pins the logical IDs)
    this.sourceFilesTable = this.advancedTable('SourceFiles', {
      partitionKey: { name: 'source_file_id', type: dynamodb.AttributeType.STRING },
      globalSecondaryIndexes: ownerIndexes,
      timeToLiveAttribute: 'expires_at',
    });
    this.advancedGenerationsTable = this.advancedTable('AdvancedGenerations', {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      globalSecondaryIndexes: ownerIndexes,
      timeToLiveAttribute: 'expires_at',
    });
    // Administrator settings get only PITR and RETAIN, no deletion protection (the app only reads them)
    this.appSettingsTable = this.advancedTable('AppSettings', {
      partitionKey: { name: 'setting_key', type: dynamodb.AttributeType.STRING },
      deletionProtection: false,
    });
  }

  /** An advanced generation table: on-demand, AWS managed key, PITR and RETAIN. */
  private advancedTable(
    id: string,
    options: {
      partitionKey: dynamodb.Attribute;
      sortKey?: dynamodb.Attribute;
      globalSecondaryIndexes?: dynamodb.GlobalSecondaryIndexPropsV2[];
      timeToLiveAttribute?: string;
      deletionProtection?: boolean;
    },
  ): dynamodb.TableV2 {
    const { deletionProtection = true, globalSecondaryIndexes, ...keys } = options;
    const table = new dynamodb.TableV2(this, id, {
      ...keys,
      billing: dynamodb.Billing.onDemand(),
      encryption: dynamodb.TableEncryptionV2.awsManagedKey(),
      globalSecondaryIndexes,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      ...(deletionProtection ? { deletionProtection: true } : {}),
      removalPolicy: RemovalPolicy.RETAIN,
    });
    this.recordIndexes(table, globalSecondaryIndexes ?? []);
    return table;
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

  /** The four discussion and interview tables (the App stack grants narrowed actions in one statement). */
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
    const table = new dynamodb.TableV2(this, id, {
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
    this.recordIndexes(table, globalSecondaryIndexes ?? []);
    return table;
  }

  private recordIndexes(
    table: dynamodb.TableV2,
    indexes: readonly dynamodb.GlobalSecondaryIndexPropsV2[],
  ): void {
    this.indexNames.set(table, indexes.map((index) => index.indexName));
  }

  /**
   * ARNs of the GSIs on `table` that the application queries (`<tableArn>/index/<name>`).
   *
   * Only indexes defined on this table and listed in APP_QUERIED_INDEXES are returned, so the
   * task role never gets `<tableArn>/index/*`. Tables without such an index return [].
   */
  public queriedIndexArns(table: dynamodb.TableV2): string[] {
    return (this.indexNames.get(table) ?? [])
      .filter((name) => APP_QUERIED_INDEXES.has(name))
      .map((name) => `${table.tableArn}/index/${name}`);
  }

  /**
   * The 11 tables the App stack grants the task role access to and passes by name through
   * environment variables. The old bindings tables (PersonaDatasetBindings, PersonaKBBindings)
   * are excluded; they remain in AWS because of RETAIN. Includes the three advanced generation
   * (beta) tables (keys are *_TABLE_NAME in src/config.py).
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
      SOURCE_FILES_TABLE_NAME: this.sourceFilesTable,
      ADVANCED_GENERATIONS_TABLE_NAME: this.advancedGenerationsTable,
      APP_SETTINGS_TABLE_NAME: this.appSettingsTable,
    };
  }
}
