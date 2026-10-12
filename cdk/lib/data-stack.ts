import { Construct } from 'constructs';
import { CfnOutput, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { AppParameter } from '../parameters';
import { AgentCoreMemory } from './constructs/agentcore-memory';
import { Cognito } from './constructs/cognito';
import { Database } from './constructs/database';
import { EcrRepository } from './constructs/ecr';
import { UploadBucket } from './constructs/upload-bucket';

export interface DataStackProps extends StackProps {
  parameter: AppParameter;
}

/**
 * Data スタック: 状態を持つもの（FR9.1、ADR-001、約束 C10）。
 *
 * テーブル・S3・ECR・AgentCore Memory・Cognito User Pool・KMS キー・
 * トークンの暗号化鍵とログイン途中の Cookie の署名鍵を持つ。すべて RETAIN。
 * App スタックはここから参照で受け取る（App → Data の一方向）。
 */
export class DataStack extends Stack {
  public readonly database: Database;
  public readonly uploadBucket: UploadBucket;
  public readonly ecr: EcrRepository;
  public readonly memory: AgentCoreMemory;
  public readonly cognito: Cognito;
  public readonly tokenEncryptionKeys: secretsmanager.Secret;
  public readonly loginCookieSigningKey: secretsmanager.Secret;

  constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, props);

    const { parameter } = props;

    this.database = new Database(this, 'Database');
    this.uploadBucket = new UploadBucket(this, 'UploadBucket');
    this.ecr = new EcrRepository(this, 'Ecr');
    this.memory = new AgentCoreMemory(this, 'Memory', {
      namePrefix: 'AIPersona_v1',
      envName: parameter.envName,
      eventExpiryDuration: parameter.agentCoreMemoryEventExpiryDays ?? 90,
      description: `Long-term memory for AI Persona v1 ${parameter.envName}`,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    this.cognito = new Cognito(this, 'Cognito', { envName: parameter.envName });

    // トークンの暗号化鍵の一覧（security-design.md「暗号化と鍵」）。CDK は初期値を
    // 作るだけで、版の追加・切り替えは手で値を書き換える。
    this.tokenEncryptionKeys = new secretsmanager.Secret(this, 'TokenEncryptionKeys', {
      description: 'AI Persona v1 token encryption keys ({"current": "v1", "k_v1": ...})',
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ current: 'v1' }),
        generateStringKey: 'k_v1',
        passwordLength: 64,
        excludePunctuation: true,
      },
      removalPolicy: RemovalPolicy.RETAIN,
    });
    this.loginCookieSigningKey = new secretsmanager.Secret(this, 'LoginCookieSigningKey', {
      description: 'AI Persona v1 signing key of the login transaction cookie',
      generateSecretString: { passwordLength: 64, excludePunctuation: true },
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // deploy.sh がイメージを送る先と、最初の admin を作る手順の表示に使う
    new CfnOutput(this, 'EcrRepositoryUri', { value: this.ecr.repository.repositoryUri });
    new CfnOutput(this, 'UserPoolId', { value: this.cognito.userPool.userPoolId });
    new CfnOutput(this, 'AgentCoreMemoryId', { value: this.memory.memoryId });
  }
}
