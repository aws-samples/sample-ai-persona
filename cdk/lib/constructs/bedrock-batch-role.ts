import { Construct } from 'constructs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';

export interface BedrockBatchRoleProps {
  bucket: s3.IBucket;
}

export class BedrockBatchRole extends Construct {
  public readonly role: iam.Role;

  constructor(scope: Construct, id: string, props: BedrockBatchRoleProps) {
    super(scope, id);

    const { bucket } = props;

    this.role = new iam.Role(this, 'Role', {
      roleName: 'BedrockBatchInferenceRole',
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

    // Bedrock model invocation
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockInvokeModel',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: ['*'],
      })
    );
  }
}
