import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';

export interface CloudFrontDistributionProps {
  /** ALB ARN from Express Mode */
  loadBalancerArn: string;
  /** Express Mode endpoint domain */
  expressEndpoint: string;
  envName: string;
  enableWaf?: boolean;
}

export class CloudFrontDistribution extends Construct {
  public readonly distribution: cloudfront.Distribution;
  public readonly domainName: string;

  constructor(scope: Construct, id: string, props: CloudFrontDistributionProps) {
    super(scope, id);

    const { loadBalancerArn, expressEndpoint, envName, enableWaf = true } = props;

    // VPC Origin via L1 (Express Mode ALB ARN is a deploy-time token)
    const cfnVpcOrigin = new cloudfront.CfnVpcOrigin(this, 'VpcOrigin', {
      vpcOriginEndpointConfig: {
        arn: loadBalancerArn,
        httpPort: 80,
        httpsPort: 443,
        originProtocolPolicy: 'https-only',
        originSslProtocols: ['TLSv1.2'],
        name: `ai-persona-origin-${envName}`,
      },
    });

    const vpcOrigin = cloudfront.VpcOrigin.fromVpcOriginAttributes(this, 'VpcOriginL2', {
      vpcOriginId: cfnVpcOrigin.attrId,
      domainName: expressEndpoint,
    });

    // WAF WebACL
    let webAclArn: string | undefined;
    if (enableWaf) {
      const webAcl = new wafv2.CfnWebACL(this, 'WebAcl', {
        defaultAction: { allow: {} },
        scope: 'CLOUDFRONT',
        visibilityConfig: {
          cloudWatchMetricsEnabled: true,
          metricName: `ai-persona-waf-${envName}`,
          sampledRequestsEnabled: true,
        },
        name: `ai-persona-waf-${envName}`,
        rules: [
          {
            name: 'AWSManagedRulesCommonRuleSet',
            priority: 1,
            overrideAction: { none: {} },
            statement: {
              managedRuleGroupStatement: { vendorName: 'AWS', name: 'AWSManagedRulesCommonRuleSet' },
            },
            visibilityConfig: { cloudWatchMetricsEnabled: true, metricName: 'AWSManagedRulesCommonRuleSet', sampledRequestsEnabled: true },
          },
          {
            name: 'RateLimit',
            priority: 2,
            action: { block: {} },
            statement: { rateBasedStatement: { limit: 2000, aggregateKeyType: 'IP' } },
            visibilityConfig: { cloudWatchMetricsEnabled: true, metricName: 'RateLimit', sampledRequestsEnabled: true },
          },
        ],
      });
      webAclArn = webAcl.attrArn;
    }

    // CloudFront Distribution — HTTPS to Express Mode ALB
    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: `AI Persona - ${envName}`,
      defaultBehavior: {
        origin: origins.VpcOrigin.withVpcOrigin(vpcOrigin),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      },
      webAclId: webAclArn,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
    });

    this.domainName = this.distribution.distributionDomainName;

    new cdk.CfnOutput(this, 'DistributionDomainName', {
      value: this.distribution.distributionDomainName,
      description: 'CloudFront Distribution Domain Name',
    });
    new cdk.CfnOutput(this, 'DistributionId', {
      value: this.distribution.distributionId,
      description: 'CloudFront Distribution ID',
    });
  }
}
