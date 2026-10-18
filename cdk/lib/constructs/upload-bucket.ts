import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import { acknowledgeAll, NAG_REASONS } from '../nag-reasons';

/**
 * アップロードファイル用の S3 バケット（v1.0.0、Data スタック）。
 *
 * 物理名は自動生成にし（v0 のバケットと並べて置けるように）、RETAIN にする。
 * オブジェクトの自動削除（autoDeleteObjects）は使わない（project.md の Forbidden）。
 *
 * U5 のライフサイクル（infrastructure-specification.md、NFR1.33、NFR1.41）:
 * - `candidates/` の生成候補の一時 CSV は 1 日で失効（候補そのものは 8 時間）
 * - タグ `pending=true`（紐付く前のアップロード）は 1 日で失効（使えるのは 24 時間）
 * - 古いバージョンは 7 日で削除、期限切れの削除マーカーを掃除、未完了のマルチパートは 7 日で中止
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
      lifecycleRules: [
        {
          id: 'ExpireCandidateCsv',
          prefix: 'candidates/',
          expiration: Duration.days(1),
        },
        {
          id: 'ExpireUnattachedUploads',
          tagFilters: { pending: 'true' },
          expiration: Duration.days(1),
        },
        {
          id: 'ExpireNoncurrentVersions',
          noncurrentVersionExpiration: Duration.days(7),
          expiredObjectDeleteMarker: true,
          abortIncompleteMultipartUploadAfter: Duration.days(7),
        },
      ],
    });
    acknowledgeAll(this.bucket, ['S1'], NAG_REASONS.accessLogs);
  }
}
