import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Duration, RemovalPolicy } from 'aws-cdk-lib';
import { acknowledgeAll, NAG_REASONS } from '../nag-reasons';

/**
 * Number of days an uploaded file remains usable for advanced generation. Must match
 * ADVANCED_SOURCE_RETENTION_DAYS in src/config.py (checked by tests/infra/test_source_retention.py).
 */
export const SOURCE_RETENTION_DAYS = 7;
/** DynamoDB TTL deletion can lag up to ~2 days, so S3 expiry of the current version waits that long. */
export const SOURCE_LIFECYCLE_MARGIN_DAYS = 2;

/**
 * S3 bucket for uploaded files (v1.0.0, Data stack).
 *
 * The physical name is auto-generated (so it can coexist with the v0 bucket) and RETAIN is set.
 * Automatic object deletion (autoDeleteObjects) is not used (forbidden in this project).
 *
 * Lifecycle:
 * - Temporary CSVs of generated candidates under `candidates/` expire in 1 day (the candidates themselves in 8 hours)
 * - Objects tagged `pending=true` (uploads not yet attached) expire in 1 day (usable for 24 hours)
 * - Old versions are removed after 7 days, expired delete markers are cleaned up, incomplete multipart uploads abort after 7 days
 * - Survey run input/output under `survey-runs/` expires in 7 days, old versions after 1 day
 * - Advanced generation (beta): incomplete multipart uploads under `sources/` abort after 1 day and old versions go after 1 day.
 *   The current version expires after 9 days.
 *   Old versions under `analysis-records/` go after 1 day. Encryption (SSE-S3) is unchanged
 *
 * CORS is not set here: the allowed origin is the CloudFront domain of the App stack, so a custom resource in the
 * App stack sets it with PutBucketCors (no Data -> App reference).
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
        // Survey run input/output (persona attributes, request text and answers). The app deletes it when done.
        // As a safety net for missed deletions, the current version goes after 7 days and old versions after 1 day.
        // This overlaps the bucket-wide rule above (old versions 7 days), but S3 honors the shorter one
        {
          id: 'ExpireSurveyRuns',
          prefix: 'survey-runs/',
          expiration: Duration.days(7),
          noncurrentVersionExpiration: Duration.days(1),
        },
        // Advanced generation (beta): multipart uploads from the browser. Abort stalled parts
        // after 1 day and remove old versions of deleted files after 1 day.
        // Expire the current version 9 days after creation (7 days retention + 2 days margin, the DynamoDB TTL lag). Each use in
        // generation makes the app copy the object onto the same key to refresh its creation date. The rule ID stays unchanged
        {
          id: 'AbortIncompleteSourceUploads',
          prefix: 'sources/',
          abortIncompleteMultipartUploadAfter: Duration.days(1),
          noncurrentVersionExpiration: Duration.days(1),
          expiration: Duration.days(SOURCE_RETENTION_DAYS + SOURCE_LIFECYCLE_MARGIN_DAYS),
        },
        // Remove old versions of analysis records after 1 day. Records before saving (tag pending=true)
        // are removed by ExpireUnattachedUploads above
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
