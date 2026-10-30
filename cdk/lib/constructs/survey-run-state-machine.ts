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
 * アンケートのステートマシン（基盤設計 2.2・3.1、Standard、Distributed Map）。
 *
 * - 名前は指定しない（CDK が論理 ID から作る）。RETAIN のステートマシンとロググループが作成の失敗や
 *   切り戻しのあとに残っても、同じ名前で作り直して「すでに存在する」で失敗しないようにする
 * - CDK の StateMachine は削除の方針を指定しないと DESTROY になるため、RETAIN を明示する
 *   （project.md の Forbidden、基盤設計のレビュー R-08）
 * - ログは ERROR だけで入出力を含めない（NFR2.3、NFR5.11、NFR7.5）。X-Ray は使わない
 * - 実行ロールの文は基盤設計 3.1 の表のとおり。ASL を直接書くため L2 は権限を足さない
 *   （ログの出力の権限だけは CDK が足す）。自分の ARN を指す文はロールの外の Policy に置き、
 *   ステートマシン → ロール → ステートマシンの循環を作らない
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
    // 推論はアンケートのモデルだけ（NFR5.3）
    this.role.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: modelArns,
    }));
    // 入力の JSONL・画像ありの Body・ResultWriter のマルチパートの読み取り。GetObjectVersion は付けない
    // （消したあとの古いバージョンを読ませない、NFR5.14a）。ListBucket も付けない（1 つのオブジェクトを読むだけ）
    this.role.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject'],
      resources: [bucket.arnForObjects('survey-runs/*')],
    }));
    // ResultWriter の書き込み（NFR5.4）
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
    // Distributed Map が子の実行を始めて見守る（NFR5.5。Step Functions 開発者ガイド「IAM policies for
    // using Distributed Map states」の最小の例と同じ）。RedriveExecution・Map Run の操作は付けない
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
