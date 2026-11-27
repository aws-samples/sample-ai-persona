import { Construct } from 'constructs';
import { CfnOutput, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { AppParameter } from '../parameters';
import { AgentCoreMemory } from './constructs/agentcore-memory';
import { Cognito } from './constructs/cognito';
import { Database } from './constructs/database';
import { EcrRepository } from './constructs/ecr';
import { UploadBucket } from './constructs/upload-bucket';
import { acknowledgeAll, NAG_REASONS } from './nag-reasons';

export interface DataStackProps extends StackProps {
  parameter: AppParameter;
  /**
   * Whether to create the Cognito Managed Login domain (default true; ResolvedProfile.createCognitoDomain).
   * false in the closed configuration.
   */
  createCognitoDomain?: boolean;
}

/**
 * Data stack: everything that holds state.
 *
 * Holds tables, S3, ECR, AgentCore Memory, the Cognito User Pool, KMS keys, the
 * token encryption keys and the signing key for the in-login cookie. All RETAIN.
 * The App stack receives them by reference (one way, App -> Data).
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
    this.cognito = new Cognito(this, 'Cognito', {
      envName: parameter.envName,
      createDomain: props.createCognitoDomain ?? true,
    });

    // List of token encryption keys. CDK only creates the initial value;
    // adding and switching versions is done by rewriting the value by hand.
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
    // The two keys are rotated by adding versions by hand (SMG4 suppression)
    for (const secret of [this.tokenEncryptionKeys, this.loginCookieSigningKey]) {
      acknowledgeAll(secret, ['SMG4'], NAG_REASONS.manualKeyRotation);
    }

    // Used by deploy.sh for the image push target and the first-admin instructions
    new CfnOutput(this, 'EcrRepositoryUri', { value: this.ecr.repository.repositoryUri });
    new CfnOutput(this, 'UserPoolId', { value: this.cognito.userPool.userPoolId });
    new CfnOutput(this, 'AgentCoreMemoryId', { value: this.memory.memoryId });
  }
}
