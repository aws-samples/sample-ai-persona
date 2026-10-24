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

function buildService(
  overrides: Partial<ExpressServiceProps>,
): { stack: cdk.Stack; service: ExpressService } {
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

  const service = new ExpressService(stack, 'Service', {
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
  return { stack, service };
}

function synthEnvironment(
  overrides: Partial<ExpressServiceProps>,
): { Name: string; Value: string }[] {
  const { stack } = buildService(overrides);
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

/**
 * Express の Endpoint 属性は `https://` 付きで返ることがある（2026-10-23 の dev デプロイで
 * CloudFront が "origin name cannot contain a colon" で失敗した）。CloudFront のオリジンに
 * 渡す endpoint は、付いていてもいなくてもホスト名だけになることを検証する。
 */

/** Evaluate the Fn::Join / Fn::Split / Fn::GetAtt subset with a given Endpoint value. */
function evaluate(expr: any, endpoint: string): any {
  if (typeof expr === 'string') return expr;
  if (expr['Fn::GetAtt']) return endpoint;
  if (expr['Fn::Split']) {
    const [delimiter, source] = expr['Fn::Split'];
    return (evaluate(source, endpoint) as string).split(delimiter);
  }
  if (expr['Fn::Join']) {
    const [delimiter, parts] = expr['Fn::Join'];
    const values = Array.isArray(parts) ? parts.map((p: any) => evaluate(p, endpoint)) : evaluate(parts, endpoint);
    return values.flat().join(delimiter);
  }
  throw new Error(`unsupported expression: ${JSON.stringify(expr)}`);
}

describe('ExpressService endpoint', () => {
  function endpointExpression(): any {
    const { stack, service } = buildService({});
    new cdk.CfnOutput(stack, 'EndpointOut', { value: service.endpoint });
    return Template.fromStack(stack).toJSON().Outputs.EndpointOut.Value;
  }

  test('Endpoint 属性を参照している', () => {
    expect(JSON.stringify(endpointExpression())).toContain('"Endpoint"');
  });

  test.each([
    ['https:// 付き', 'https://ai-xxxx.ecs.us-east-1.on.aws'],
    ['ホスト名だけ', 'ai-xxxx.ecs.us-east-1.on.aws'],
  ])('%s → ホスト名だけになる', (_label, raw) => {
    const host = evaluate(endpointExpression(), raw);
    expect(host).toBe('ai-xxxx.ecs.us-east-1.on.aws');
    expect(host).not.toContain(':');
  });
});
