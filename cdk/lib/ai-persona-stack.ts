import { Construct } from 'constructs';
import { Stack, StackProps, CfnOutput, RemovalPolicy } from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Database } from './constructs/database';
import { UploadBucket } from './constructs/upload-bucket';
import { ExpressService } from './constructs/express-service';
import { BedrockBatchRole } from './constructs/bedrock-batch-role';
import { Vpc } from './constructs/vpc';
import { AppParameter } from '../parameters';

export interface AIPersonaStackProps extends StackProps {
  parameter: AppParameter;
  ecrRepository: ecr.IRepository;
}

export class AIPersonaStack extends Stack {
  public readonly serviceEndpoint: string;

  constructor(scope: Construct, id: string, props: AIPersonaStackProps) {
    super(scope, id, props);

    const { parameter, ecrRepository } = props;
    const isProd = parameter.envName === 'prod';

    // Database (DynamoDB)
    const database = new Database(this, 'Database', {
      tablePrefix: parameter.dynamoDbTablePrefix,
      removalPolicy: isProd ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    });

    // VPC
    const { vpc } = new Vpc(this, 'Vpc', {
      envName: parameter.envName,
    });

    // S3 Bucket for file uploads
    const uploadBucket = new UploadBucket(this, 'UploadBucket', {
      bucketNamePrefix: parameter.dynamoDbTablePrefix,
      accountId: this.account,
      removalPolicy: isProd ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    });

    // Bedrock Batch Inference IAM Role
    const bedrockBatchRole = new BedrockBatchRole(this, 'BedrockBatchRole', {
      bucket: uploadBucket.bucket,
    });

    // AgentCore Memory IDとStrategy IDはparameters.tsから取得
    // AgentCoreMemoryStackを先にデプロイして、IDを取得してください
    const agentCoreMemoryId = parameter.agentCoreMemoryId;
    const summaryMemoryStrategyId = parameter.summaryMemoryStrategyId;
    const semanticMemoryStrategyId = parameter.semanticMemoryStrategyId;

    // IDが設定されていない場合は警告
    if (!agentCoreMemoryId || !summaryMemoryStrategyId) {
      console.warn('WARNING: AgentCore Memory IDs are not configured in parameters.ts');
      console.warn('Please deploy AgentCoreMemoryStack first and update parameters.ts');
    }
    if (!semanticMemoryStrategyId) {
      console.warn('WARNING: Semantic Memory Strategy ID is not configured in parameters.ts');
    }

    // ECS Express Mode Service
    const service = new ExpressService(this, 'Service', {
      vpc,
      ecrRepository,
      envName: parameter.envName,
      containerCpu: parameter.containerCpu,
      containerMemory: parameter.containerMemory,
      dynamoDbTables: [
        database.personasTable,
        database.discussionsTable,
        database.uploadedFilesTable,
        database.datasetsTable,
        database.bindingsTable,
        database.surveyTemplatesTable,
        database.surveysTable,
        database.knowledgeBasesTable,
        database.personaKBBindingsTable,
      ],
      dynamoDbTablePrefix: parameter.dynamoDbTablePrefix,
      awsRegion: this.region,
      bedrockModelId: parameter.bedrockModelId,
      agentModelId: parameter.agentModelId,
      agentCoreMemoryId,
      summaryMemoryStrategyId,
      semanticMemoryStrategyId,
      uploadBucket: uploadBucket.bucket,
      bedrockBatchRoleArn: bedrockBatchRole.role.roleArn,
      batchInferenceModelId: parameter.batchInferenceModelId,
      surveyS3Prefix: parameter.surveyS3Prefix,
      batchInferenceS3Prefix: parameter.batchInferenceS3Prefix,
    });

    this.serviceEndpoint = service.endpoint;

    // Outputs
    new CfnOutput(this, 'ServiceEndpoint', {
      value: service.endpoint,
      description: 'ECS Express Service Endpoint',
      exportName: `${id}-ServiceEndpoint`,
    });

    new CfnOutput(this, 'PersonasTableName', {
      value: database.personasTable.tableName,
      description: 'DynamoDB Personas Table Name',
    });

    new CfnOutput(this, 'DiscussionsTableName', {
      value: database.discussionsTable.tableName,
      description: 'DynamoDB Discussions Table Name',
    });

    new CfnOutput(this, 'UploadedFilesTableName', {
      value: database.uploadedFilesTable.tableName,
      description: 'DynamoDB UploadedFiles Table Name',
    });

    new CfnOutput(this, 'UploadBucketName', {
      value: uploadBucket.bucket.bucketName,
      description: 'S3 Upload Bucket Name',
      exportName: `${id}-UploadBucketName`,
    });

    new CfnOutput(this, 'SurveyTemplatesTableName', {
      value: database.surveyTemplatesTable.tableName,
      description: 'DynamoDB SurveyTemplates Table Name',
    });

    new CfnOutput(this, 'SurveysTableName', {
      value: database.surveysTable.tableName,
      description: 'DynamoDB Surveys Table Name',
    });

    new CfnOutput(this, 'BedrockBatchRoleArn', {
      value: bedrockBatchRole.role.roleArn,
      description: 'Bedrock Batch Inference IAM Role ARN',
      exportName: `${id}-BedrockBatchRoleArn`,
    });
  }
}
