import { Construct } from 'constructs';
import { Stack, StackProps, CfnOutput } from 'aws-cdk-lib';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';

export interface WafStackProps extends StackProps {
  envName: string;
}

export class WafStack extends Stack {
  public readonly webAclArn: string;

  constructor(scope: Construct, id: string, props: WafStackProps) {
    super(scope, id, props);

    const { envName } = props;

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

    this.webAclArn = webAcl.attrArn;

    new CfnOutput(this, 'WebAclArn', {
      value: webAcl.attrArn,
      description: 'WAF WebACL ARN (for CloudFront)',
      exportName: `${id}-WebAclArn`,
    });
  }
}
