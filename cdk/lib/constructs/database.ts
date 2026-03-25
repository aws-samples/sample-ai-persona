import { Construct } from 'constructs';
import { RemovalPolicy } from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';

export interface DatabaseProps {
  tablePrefix: string;
  removalPolicy?: RemovalPolicy;
}

export class Database extends Construct {
  public readonly personasTable: dynamodb.Table;
  public readonly discussionsTable: dynamodb.Table;
  public readonly uploadedFilesTable: dynamodb.Table;
  public readonly datasetsTable: dynamodb.Table;
  public readonly bindingsTable: dynamodb.Table;
  public readonly surveyTemplatesTable: dynamodb.Table;
  public readonly surveysTable: dynamodb.Table;
  public readonly knowledgeBasesTable: dynamodb.Table;
  public readonly personaKBBindingsTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: DatabaseProps) {
    super(scope, id);

    const { tablePrefix, removalPolicy = RemovalPolicy.RETAIN } = props;

    // Personas Table
    this.personasTable = new dynamodb.Table(this, 'Personas', {
      tableName: `${tablePrefix}_Personas`,
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    this.personasTable.addGlobalSecondaryIndex({
      indexName: 'CreatedAtIndex',
      partitionKey: { name: 'type', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING },
    });

    this.personasTable.addGlobalSecondaryIndex({
      indexName: 'NameIndex',
      partitionKey: { name: 'type', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'name', type: dynamodb.AttributeType.STRING },
    });

    this.personasTable.addGlobalSecondaryIndex({
      indexName: 'OccupationIndex',
      partitionKey: { name: 'type', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'occupation', type: dynamodb.AttributeType.STRING },
    });

    // Discussions Table
    this.discussionsTable = new dynamodb.Table(this, 'Discussions', {
      tableName: `${tablePrefix}_Discussions`,
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    this.discussionsTable.addGlobalSecondaryIndex({
      indexName: 'CreatedAtIndex',
      partitionKey: { name: 'type', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING },
    });

    this.discussionsTable.addGlobalSecondaryIndex({
      indexName: 'TopicIndex',
      partitionKey: { name: 'type', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'topic', type: dynamodb.AttributeType.STRING },
    });

    this.discussionsTable.addGlobalSecondaryIndex({
      indexName: 'ModeIndex',
      partitionKey: { name: 'mode', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING },
    });

    // UploadedFiles Table
    this.uploadedFilesTable = new dynamodb.Table(this, 'UploadedFiles', {
      tableName: `${tablePrefix}_UploadedFiles`,
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    this.uploadedFilesTable.addGlobalSecondaryIndex({
      indexName: 'UploadedAtIndex',
      partitionKey: { name: 'type', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'uploaded_at', type: dynamodb.AttributeType.STRING },
    });

    // Datasets Table
    this.datasetsTable = new dynamodb.Table(this, 'Datasets', {
      tableName: `${tablePrefix}_Datasets`,
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    // PersonaDatasetBindings Table
    this.bindingsTable = new dynamodb.Table(this, 'PersonaDatasetBindings', {
      tableName: `${tablePrefix}_PersonaDatasetBindings`,
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    this.bindingsTable.addGlobalSecondaryIndex({
      indexName: 'PersonaIdIndex',
      partitionKey: { name: 'persona_id', type: dynamodb.AttributeType.STRING },
    });

    // SurveyTemplates Table
    this.surveyTemplatesTable = new dynamodb.Table(this, 'SurveyTemplates', {
      tableName: `${tablePrefix}_SurveyTemplates`,
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    // Surveys Table
    this.surveysTable = new dynamodb.Table(this, 'Surveys', {
      tableName: `${tablePrefix}_Surveys`,
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    // KnowledgeBases Table
    this.knowledgeBasesTable = new dynamodb.Table(this, 'KnowledgeBases', {
      tableName: `${tablePrefix}_KnowledgeBases`,
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    // PersonaKBBindings Table
    this.personaKBBindingsTable = new dynamodb.Table(this, 'PersonaKBBindings', {
      tableName: `${tablePrefix}_PersonaKBBindings`,
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    this.personaKBBindingsTable.addGlobalSecondaryIndex({
      indexName: 'PersonaIdIndex',
      partitionKey: { name: 'persona_id', type: dynamodb.AttributeType.STRING },
    });
  }
}
