import { Construct } from 'constructs';
import { RemovalPolicy, Stack } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import { SurveyRunSettings, surveyRunDefinition } from '../survey-run';

export interface SurveyRunStateMachineProps {
  /** The app's bucket (`survey-runs/` lives in it). */
  bucket: s3.IBucket;
  /** The survey model's ARNs (bedrock-arns.ts surveyModelArns). */
  modelArns: string[];
  settings: SurveyRunSettings;
}

/**
 * Survey state machine (Standard, Distributed Map).
 *
 * - No name is set (CDK derives it from the logical ID), so a RETAIN state machine and log group
 *   left after a failed create or rollback do not make a recreate fail with "already exists"
 * - CDK StateMachine defaults to DESTROY unless a removal policy is given, so RETAIN is explicit
 *   (RemovalPolicy.DESTROY is forbidden in this project)
 * - Logs are ERROR only and exclude input/output. X-Ray is not used
 * - Execution role statements are listed one by one. ASL is written directly, so the L2 adds no permissions
 *   (except log delivery, which CDK adds). The statement pointing at its own ARN lives in a Policy outside
 *   the role, to avoid a state machine -> role -> state machine cycle
 */
export class SurveyRunStateMachine extends Construct {
  public readonly stateMachine: sfn.StateMachine;
  public readonly role: iam.Role;
  public readonly logGroup: logs.LogGroup;
  /** `arn:<partition>:states:<region>:<account>:execution:<name>:*` (task role, endpoint policy). */
  public readonly executionsArn: string;
  /** The Distributed Map's child executions (`execution:<name>/*`, the role's own policy). */
  public readonly childExecutionsArn: string;
  /** The models the role may invoke (bedrock-arns.ts surveyModelArns). */
  public readonly modelArns: readonly string[];
  /** The role's statements that name this state machine (kept off the role's default policy). */
  public readonly executionPolicy: iam.Policy;

  constructor(scope: Construct, id: string, props: SurveyRunStateMachineProps) {
    super(scope, id);
    const { bucket, modelArns, settings } = props;
    const { partition, region, account } = Stack.of(this);
    this.modelArns = [...modelArns];

    this.logGroup = new logs.LogGroup(this, 'LogGroup', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    this.role = new iam.Role(this, 'Role', {
      assumedBy: new iam.ServicePrincipal('states.amazonaws.com'),
      description: 'AI Persona survey state machine: Bedrock InvokeModel and survey-runs/ in S3',
    });
    // Inference only for the survey model
    this.role.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: modelArns,
    }));
    // Read the input JSONL, bodies with images and ResultWriter multipart parts. No GetObjectVersion
    // (so old versions after a delete cannot be read). No ListBucket either (it reads single objects only)
    this.role.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject'],
      resources: [bucket.arnForObjects('survey-runs/*')],
    }));
    // ResultWriter writes
    this.role.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:PutObject', 's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'],
      resources: [bucket.arnForObjects('survey-runs/*/results/*')],
    }));

    this.stateMachine = new sfn.StateMachine(this, 'StateMachine', {
      stateMachineType: sfn.StateMachineType.STANDARD,
      definitionBody: sfn.DefinitionBody.fromString(JSON.stringify(surveyRunDefinition(settings))),
      role: this.role,
      logs: { destination: this.logGroup, level: sfn.LogLevel.ERROR, includeExecutionData: false },
      tracingEnabled: false,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const name = this.stateMachine.stateMachineName;
    this.executionsArn = `arn:${partition}:states:${region}:${account}:execution:${name}:*`;
    this.childExecutionsArn = `arn:${partition}:states:${region}:${account}:execution:${name}/*`;
    // The Distributed Map starts and watches child executions (same as the minimal example in the Step Functions
    // developer guide "IAM policies for using Distributed Map states"). RedriveExecution and Map Run operations are not granted
    this.executionPolicy = new iam.Policy(this, 'ExecutionPolicy', {
      roles: [this.role],
      statements: [
        new iam.PolicyStatement({
          actions: ['states:StartExecution'],
          resources: [this.stateMachine.stateMachineArn],
        }),
        new iam.PolicyStatement({
          actions: ['states:DescribeExecution'],
          resources: [this.childExecutionsArn],
        }),
      ],
    });
  }
}
