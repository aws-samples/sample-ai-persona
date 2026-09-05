import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { ExpressService, ExpressServiceProps } from '../lib/constructs/express-service';

/**
 * ENABLE_DATASET_ANALYSIS が ECS タスクの環境変数へ正しく配線されることを検証する。
 * true / false / 未指定（既定 true・"undefined" にならないこと）の3ケースを確認する。
 */

function synthEnvironment(
  overrides: Partial<ExpressServiceProps>,
): { Name: string; Value: string }[] {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'TestStack', {
    env: { account: '123456789012', region: 'us-east-1' },
  });

  const vpc = new ec2.Vpc(stack, 'Vpc', { maxAzs: 2 });
  const ecrRepository = new ecr.Repository(stack, 'Repo');
  const uploadBucket = new s3.Bucket(stack, 'Bucket');
  const table = new dynamodb.Table(stack, 'Table', {
    partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
  });

  new ExpressService(stack, 'Service', {
    vpc,
    ecrRepository,
    envName: 'test',
    containerCpu: '1024',
    containerMemory: '4096',
    dynamoDbTables: [table],
    dynamoDbTablePrefix: 'TestPrefix',
    awsRegion: 'us-east-1',
    bedrockModelId: 'model',
    agentModelId: 'agent-model',
    uploadBucket,
    bedrockBatchRoleArn: 'arn:aws:iam::123456789012:role/batch',
    batchInferenceModelId: 'batch-model',
    ...overrides,
  });

  const template = Template.fromStack(stack);
  const resources = template.toJSON().Resources as Record<string, any>;
  for (const key of Object.keys(resources)) {
    const primaryContainer = resources[key].Properties?.PrimaryContainer;
    if (primaryContainer?.Environment) {
      return primaryContainer.Environment as { Name: string; Value: string }[];
    }
  }
  throw new Error('Express service PrimaryContainer.Environment not found');
}

function datasetAnalysisValue(env: { Name: string; Value: string }[]): string | undefined {
  return env.find((e) => e.Name === 'ENABLE_DATASET_ANALYSIS')?.Value;
}

describe('ExpressService ENABLE_DATASET_ANALYSIS wiring', () => {
  test('enableDatasetAnalysis=true → "true"', () => {
    const env = synthEnvironment({ enableDatasetAnalysis: true });
    expect(datasetAnalysisValue(env)).toBe('true');
  });

  test('enableDatasetAnalysis=false → "false"', () => {
    const env = synthEnvironment({ enableDatasetAnalysis: false });
    expect(datasetAnalysisValue(env)).toBe('false');
  });

  test('未指定 → 既定 "true"（"undefined" にならない）', () => {
    const env = synthEnvironment({});
    const value = datasetAnalysisValue(env);
    expect(value).toBe('true');
    expect(value).not.toBe('undefined');
  });
});
