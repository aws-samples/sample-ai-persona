import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { acknowledgeAll, NAG_REASONS } from '../nag-reasons';

export interface CloudFrontDistributionProps {
  /** Internal ALB (VPC origin). */
  loadBalancer: elbv2.IApplicationLoadBalancer;
  /** ARN of the WAF WebACL (Edge stack, us-east-1). */
  webAclArn?: string;
}

/**
 * CloudFront (v1.0.0, App stack).
 *
 * - No Lambda@Edge (the app handles login)
 * - The default behavior is not cached (CachingDisabled); only /static/* is
 *   cached. The cache key includes the query v (a content hash added by the app, web/static_assets.py)
 *   so changed content after a deploy is fetched as a new URL. The managed
 *   CachingOptimized policy ignores the query and kept serving old JS/CSS for up to a day after deploys.
 * - The origin request policy is the AWS managed AllViewerExceptHostHeader (all cookies,
 *   all query strings and all headers except Host, including Origin, HX-Request and
 *   X-Requested-With, which CSRF checks need)
 * - Reaches the internal ALB over HTTP (port 80) via a VPC origin, with a 120 s read timeout
 */
export class CloudFrontDistribution extends Construct {
  public readonly distribution: cloudfront.Distribution;
  public readonly domainName: string;

  constructor(scope: Construct, id: string, props: CloudFrontDistributionProps) {
    super(scope, id);

    const origin = origins.VpcOrigin.withApplicationLoadBalancer(props.loadBalancer, {
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      httpPort: 80,
      readTimeout: cdk.Duration.seconds(120),
    });

    const staticAssets = new cloudfront.CachePolicy(this, 'StaticAssetsCachePolicy', {
      comment: 'AI Persona v1 static assets (versioned by ?v=<content hash>)',
      defaultTtl: cdk.Duration.days(1),
      minTtl: cdk.Duration.seconds(0),
      maxTtl: cdk.Duration.days(365),
      queryStringBehavior: cloudfront.CacheQueryStringBehavior.allowList('v'),
      headerBehavior: cloudfront.CacheHeaderBehavior.none(),
      cookieBehavior: cloudfront.CacheCookieBehavior.none(),
      enableAcceptEncodingGzip: true,
      enableAcceptEncodingBrotli: true,
    });

    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: 'AI Persona v1',
      defaultBehavior: {
        origin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      },
      additionalBehaviors: {
        '/static/*': {
          origin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: staticAssets,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        },
      },
      webAclId: props.webAclArn,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
    });
    this.domainName = this.distribution.distributionDomainName;

    // cdk-nag suppressions; reasons are in nag-reasons.ts
    acknowledgeAll(this.distribution, ['CFR1'], NAG_REASONS.geoRestriction);
    acknowledgeAll(this.distribution, ['CFR3'], NAG_REASONS.accessLogs);
    acknowledgeAll(this.distribution, ['CFR4'], NAG_REASONS.cloudFrontDefaultCertificate);
    if (!props.webAclArn) {
      // Only a configuration with enableWaf: false has no WebACL (enabled by default)
      acknowledgeAll(this.distribution, ['CFR2'], NAG_REASONS.wafOptional);
    }
  }
}
