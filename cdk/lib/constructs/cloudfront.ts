import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import * as fs from 'fs';
import * as path from 'path';

export interface CloudFrontDistributionProps {
  loadBalancerArn: string;
  expressEndpoint: string;
  envName: string;
  enableWaf?: boolean;
  cognitoRegion: string;
  cognitoUserPoolId: string;
  cognitoUserPoolAppId: string;
  cognitoUserPoolDomain: string;
}

export class CloudFrontDistribution extends Construct {
  public readonly distribution: cloudfront.Distribution;
  public readonly domainName: string;

  constructor(scope: Construct, id: string, props: CloudFrontDistributionProps) {
    super(scope, id);

    const {
      loadBalancerArn, expressEndpoint, envName, enableWaf = true,
      cognitoRegion, cognitoUserPoolId, cognitoUserPoolAppId, cognitoUserPoolDomain,
    } = props;

    // VPC Origin via L1
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

    // Lambda@Edge: inject Cognito config into index.js at synth time
    const lambdaDir = path.join(__dirname, '../../lambda/auth-at-edge');
    const indexContent = `const { Authenticator } = require('cognito-at-edge');
const authenticator = new Authenticator({
  region: '${cognitoRegion}',
  userPoolId: '${cognitoUserPoolId}',
  userPoolAppId: '${cognitoUserPoolAppId}',
  userPoolDomain: '${cognitoUserPoolDomain}',
  cookieExpirationDays: 30,
  httpOnly: true,
  sameSite: 'Lax',
  logLevel: 'warn',
});
exports.handler = async (request) => authenticator.handle(request);
`;
    fs.writeFileSync(path.join(lambdaDir, 'index.js'), indexContent);

    const authFunction = new cloudfront.experimental.EdgeFunction(this, 'AuthAtEdge', {
      functionName: `ai-persona-auth-edge-${envName}`,
      runtime: lambda.Runtime.NODEJS_20_X,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(lambdaDir),
      memorySize: 128,
      timeout: cdk.Duration.seconds(5),
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

    // Origin Request Policy (exclude Host for Express Mode ALB routing)
    const originRequestPolicy = new cloudfront.OriginRequestPolicy(this, 'OriginRequestPolicy', {
      originRequestPolicyName: `ai-persona-orp-${envName}`,
      headerBehavior: cloudfront.OriginRequestHeaderBehavior.allowList(
        'Accept', 'Accept-Language', 'Content-Type', 'Referer', 'User-Agent',
        'X-Requested-With', 'HX-Request', 'HX-Current-URL', 'HX-Target', 'HX-Trigger',
      ),
      cookieBehavior: cloudfront.OriginRequestCookieBehavior.all(),
      queryStringBehavior: cloudfront.OriginRequestQueryStringBehavior.all(),
    });

    // CloudFront Function: append trailing slash to root URL
    // Without this, https://xxx.cloudfront.net (no slash) causes redirect_mismatch
    const trailingSlashFunction = new cloudfront.Function(this, 'TrailingSlash', {
      functionName: `ai-persona-trailing-slash-${envName}`,
      code: cloudfront.FunctionCode.fromInline(`
function handler(event) {
  var request = event.request;
  if (request.uri === '') {
    request.uri = '/';
  }
  return request;
}
`),
      runtime: cloudfront.FunctionRuntime.JS_2_0,
    });

    // CloudFront Distribution
    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: `AI Persona - ${envName}`,
      defaultBehavior: {
        origin: origins.VpcOrigin.withVpcOrigin(vpcOrigin),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        functionAssociations: [{
          function: trailingSlashFunction,
          eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
        }],
        edgeLambdas: [{
          functionVersion: authFunction.currentVersion,
          eventType: cloudfront.LambdaEdgeEventType.VIEWER_REQUEST,
        }],
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
