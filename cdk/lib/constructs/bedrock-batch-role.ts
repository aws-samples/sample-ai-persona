import { Construct } from 'constructs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { bedrockInferenceArns } from '../bedrock-arns';

export interface BedrockBatchRoleProps {
  bucket: s3.IBucket;
  /**
   * IAM ロールの名前。省略時は自動生成（v1 は v0 と同じアカウントに並べて置くため、
   * 固定名 BedrockBatchInferenceRole とぶつからないようにする）。
   */
  roleName?: string;
  /**
   * InvokeModel を許す推論の ARN（閉域対応 U5 BR2.4: タスクロールと同じ一覧を App スタックが渡す）。
   * 省略時は公開（global）の 3 つ。
   */
  inferenceArns?: string[];
}

export class BedrockBatchRole extends Construct {
  public readonly role: iam.Role;

  constructor(scope: Construct, id: string, props: BedrockBatchRoleProps) {
    super(scope, id);

    const { bucket, roleName } = props;
    const inferenceArns = props.inferenceArns ?? bedrockInferenceArns(this);

    this.role = new iam.Role(this, 'Role', {
      roleName,
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com'),
      description: 'IAM role for Amazon Bedrock Batch Inference to access S3 input/output',
    });

    // S3 read access for input
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'S3ReadInput',
        effect: iam.Effect.ALLOW,
        actions: ['s3:GetObject', 's3:ListBucket'],
        resources: [bucket.bucketArn, `${bucket.bucketArn}/batch-inference/*`],
      })
    );

    // S3 write access for output
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'S3WriteOutput',
        effect: iam.Effect.ALLOW,
        actions: ['s3:PutObject', 's3:GetObject'],
        resources: [`${bucket.bucketArn}/batch-inference/output/*`],
      })
    );

    // Bedrock model invocation（U9、NFR7.16: 推論の ARN に絞る。U5: 範囲で変わる）
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockInvokeModel',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: inferenceArns,
      })
    );
  }
}
