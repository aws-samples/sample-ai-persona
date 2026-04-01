import { Construct } from 'constructs';
import { Stack, StackProps, CfnOutput, RemovalPolicy } from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Database } from './constructs/database';
import { UploadBucket } from './constructs/upload-bucket';
import { ExpressService } from './constructs/express-service';
import { BedrockBatchRole } from './constructs/bedrock-batch-role';
import { Vpc } from './constructs/vpc';
import { CloudFrontDistribution } from './constructs/cloudfront';
import { AppParameter } from '../parameters';

export interface AIPersonaStackProps extends StackProps {
  parameter: AppParameter;
  ecrRepository: ecr.IRepository;
}

export class AIPersonaStack extends Stack {
  public readonly cloudFrontDomainName: string;
  public readonly serviceEndpoint: string;

  constructor(scope: Construct, id: string, props: AIPersonaStackProps) {
    super(scope, id, props);

    const { parameter, ecrRepository } = props;
    const isProd = parameter.envName === 'prod';

    const database = new Database(this, 'Database', {
      tablePrefix: parameter.dynamoDbTablePrefix,
      removalPolicy: isProd ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    });

    const { vpc } = new Vpc(this, 'Vpc', { envName: parameter.envName });

    const uploadBucket = new UploadBucket(this, 'UploadBucket', {
      bucketNamePrefix: parameter.dynamoDbTablePrefix,
      accountId: this.account,
      removalPolicy: isProd ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    });

    const bedrockBatchRole = new BedrockBatchRole(this, 'BedrockBatchRole', {
      bucket: uploadBucket.bucket,
    });

    const agentCoreMemoryId = parameter.agentCoreMemoryId;
    const summaryMemoryStrategyId = parameter.summaryMemoryStrategyId;
    const semanticMemoryStrategyId = parameter.semanticMemoryStrategyId;

    if (!agentCoreMemoryId || !summaryMemoryStrategyId) {
      console.warn('WARNING: AgentCore Memory IDs are not configured in parameters.ts');
    }

    // ECS Express Mode (Private Subnet → Internal ALB with auto HTTPS)
    const service = new ExpressService(this, 'Service', {
      vpc,
      ecrRepository,
      envName: parameter.envName,
      containerCpu: parameter.containerCpu,
      containerMemory: parameter.containerMemory,
      dynamoDbTables: [
        database.personasTable, database.discussionsTable, database.uploadedFilesTable,
        database.datasetsTable, database.bindingsTable,
        database.surveyTemplatesTable, database.surveysTable,
        database.knowledgeBasesTable, database.personaKBBindingsTable,
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

    // CloudFront + VPC Origin + WAF
    const cdn = new CloudFrontDistribution(this, 'CloudFront', {
      loadBalancerArn: service.loadBalancerArn,
      expressEndpoint: service.endpoint,
      envName: parameter.envName,
      enableWaf: parameter.enableWaf,
    });

    this.cloudFrontDomainName = cdn.domainName;
    this.serviceEndpoint = service.endpoint;

    // --- Outputs ---
    new CfnOutput(this, 'CloudFrontDomainName', {
      value: cdn.domainName,
      description: 'CloudFront Domain Name (primary access point)',
      exportName: `${id}-CloudFrontDomainName`,
    });
    new CfnOutput(this, 'InternalServiceEndpoint', {
      value: service.endpoint,
      description: 'Express Mode Internal Endpoint',
      exportName: `${id}-InternalServiceEndpoint`,
    });

    // Outputs for manual Cognito-ALB auth setup
    new CfnOutput(this, 'ManagedALBArn', {
      value: service.loadBalancerArn,
      description: 'Express Mode managed ALB ARN (for manual Cognito auth setup)',
    });
    new CfnOutput(this, 'ManagedListenerArn', {
      value: service.listenerArn,
      description: 'Express Mode managed Listener ARN (for manual Cognito auth rule)',
    });
    new CfnOutput(this, 'ManagedCertificateArn', {
      value: service.certificateArn,
      description: 'Express Mode managed ACM Certificate ARN',
    });

    new CfnOutput(this, 'PersonasTableName', { value: database.personasTable.tableName });
    new CfnOutput(this, 'DiscussionsTableName', { value: database.discussionsTable.tableName });
    new CfnOutput(this, 'UploadedFilesTableName', { value: database.uploadedFilesTable.tableName });
    new CfnOutput(this, 'UploadBucketName', {
      value: uploadBucket.bucket.bucketName,
      exportName: `${id}-UploadBucketName`,
    });
    new CfnOutput(this, 'SurveyTemplatesTableName', { value: database.surveyTemplatesTable.tableName });
    new CfnOutput(this, 'SurveysTableName', { value: database.surveysTable.tableName });
    new CfnOutput(this, 'BedrockBatchRoleArn', {
      value: bedrockBatchRole.role.roleArn,
      exportName: `${id}-BedrockBatchRoleArn`,
    });
  }
}
