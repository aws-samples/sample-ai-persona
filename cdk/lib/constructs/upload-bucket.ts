import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { RemovalPolicy } from 'aws-cdk-lib';

export interface UploadBucketProps {
  bucketNamePrefix: string;
  accountId: string;
  removalPolicy: RemovalPolicy;
}

export class UploadBucket extends Construct {
  public readonly bucket: s3.IBucket;

  constructor(scope: Construct, id: string, props: UploadBucketProps) {
    super(scope, id);

    const { bucketNamePrefix, accountId, removalPolicy } = props;

    // S3 Bucket for file uploads
    this.bucket = new s3.Bucket(this, 'Bucket', {
      bucketName: `${bucketNamePrefix.toLowerCase()}-uploads-${accountId}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy,
      autoDeleteObjects: removalPolicy === RemovalPolicy.DESTROY,
    });
  }
}
