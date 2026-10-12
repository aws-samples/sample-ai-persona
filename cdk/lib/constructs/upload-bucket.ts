import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { RemovalPolicy } from 'aws-cdk-lib';

/**
 * アップロードファイル用の S3 バケット（v1.0.0、Data スタック）。
 *
 * 物理名は自動生成にし（v0 のバケットと並べて置けるように）、RETAIN にする。
 * オブジェクトの自動削除（autoDeleteObjects）は使わない（project.md の Forbidden）。
 */
export class UploadBucket extends Construct {
  public readonly bucket: s3.Bucket;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    this.bucket = new s3.Bucket(this, 'Bucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
  }
}
