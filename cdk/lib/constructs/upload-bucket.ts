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
 * - `survey-runs/` のアンケートの実行の入出力は 7 日で失効、古いバージョンは 1 日（NFR5.14）
 * - 高度な生成（ベータ、U1）: `sources/` の未完了のマルチパートは 1 日で中止、古いバージョンは 1 日。
 *   `analysis-records/` の古いバージョンは 1 日。暗号化（SSE-S3）は変えない（NFR3.2）
 *
 * CORS はここに書かない: 許すオリジンは App スタックの CloudFront のドメインのため、App スタックの
 * カスタムリソースが PutBucketCors で設定する（Data → App の参照を作らない、NFR3.10）。
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
        // アンケートの実行の入出力（ペルソナの属性・依頼の本文・回答が入る）。アプリは終わったら消す
        // （BR10.1）。消し損ねの備えに今の版を 7 日、消したあとの古いバージョンを 1 日で消す（NFR5.14）。
        // 上の全体の規則（古いバージョン 7 日）と重なるが、S3 は短い方を守る
        {
          id: 'ExpireSurveyRuns',
          prefix: 'survey-runs/',
          expiration: Duration.days(7),
          noncurrentVersionExpiration: Duration.days(1),
        },
        // 高度な生成（ベータ、U1、NFR5.5）: ブラウザからの分割アップロード。途中で止まったパートを
        // 1 日で中止し、消したファイルの古いバージョンを 1 日で消す。今の版はアプリが消す
        {
          id: 'AbortIncompleteSourceUploads',
          prefix: 'sources/',
          abortIncompleteMultipartUploadAfter: Duration.days(1),
          noncurrentVersionExpiration: Duration.days(1),
        },
        // 分析の記録の古いバージョンを 1 日で消す。保存前の記録（タグ pending=true）は上の
        // ExpireUnattachedUploads で消える（infrastructure-specification.md「決めたこと」）
        {
          id: 'ExpireNoncurrentAnalysisRecords',
          prefix: 'analysis-records/',
          noncurrentVersionExpiration: Duration.days(1),
        },
      ],
    });
    acknowledgeAll(this.bucket, ['S1'], NAG_REASONS.accessLogs);
  }
}
