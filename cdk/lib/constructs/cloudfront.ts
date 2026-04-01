import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as fs from 'fs';
import * as path from 'path';

export interface CloudFrontDistributionProps {
  loadBalancerArn: string;
  expressEndpoint: string;
  envName: string;
  /** WAF WebACL ARN (from WafStack in us-east-1) */
  webAclArn?: string;
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
      loadBalancerArn, expressEndpoint, envName, webAclArn,
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

    // CloudFront Distribution
    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: `AI Persona - ${envName}`,
      defaultBehavior: {
        origin: origins.VpcOrigin.withVpcOrigin(vpcOrigin),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
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
