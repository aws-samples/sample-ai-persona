import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';

export interface CloudFrontDistributionProps {
  /** 内部 ALB（VPC オリジン）。 */
  loadBalancer: elbv2.IApplicationLoadBalancer;
  /** WAF WebACL の ARN（Edge スタック、us-east-1）。 */
  webAclArn?: string;
}

/**
 * CloudFront（v1.0.0、App スタック）。
 *
 * - Lambda@Edge は使わない（ログインはアプリが行う。ADR-010）
 * - 既定の振る舞いはキャッシュしない（CachingDisabled、BR1.20）。/static/* だけ
 *   キャッシュする（CachingOptimized）
 * - オリジンリクエストポリシーは AWS 管理の AllViewerExceptHostHeader（全 Cookie・
 *   全クエリ・Host 以外の全ヘッダー。CSRF の検査に要る Origin・HX-Request・
 *   X-Requested-With を含む）
 * - 内部 ALB へは VPC オリジンで HTTP（ポート 80）、読み取りタイムアウト 120 秒
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
          cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        },
      },
      webAclId: props.webAclArn,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
    });
    this.domainName = this.distribution.distributionDomainName;
  }
}
