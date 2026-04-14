import { Construct } from 'constructs';
import { Stack, StackProps, CfnOutput } from 'aws-cdk-lib';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';

export interface WafStackProps extends StackProps {
  envName: string;
  allowedIpAddresses?: string[];
}

export class WafStack extends Stack {
  public readonly webAclArn: string;

  constructor(scope: Construct, id: string, props: WafStackProps) {
    super(scope, id, props);

    const { envName, allowedIpAddresses } = props;
    const hasIpRestriction = allowedIpAddresses && allowedIpAddresses.length > 0;

    // IP制限用のIPセットとルール
    const ipRules: wafv2.CfnWebACL.RuleProperty[] = [];
    if (hasIpRestriction) {
      const ipSet = new wafv2.CfnIPSet(this, 'AllowedIpSet', {
        name: `ai-persona-allowed-ips-${envName}`,
        scope: 'CLOUDFRONT',
        ipAddressVersion: 'IPV4',
        addresses: allowedIpAddresses,
      });

      ipRules.push({
        name: 'AllowedIpAddresses',
        priority: 0,
        action: { allow: {} },
        statement: {
          ipSetReferenceStatement: { arn: ipSet.attrArn },
        },
        visibilityConfig: { cloudWatchMetricsEnabled: true, metricName: 'AllowedIpAddresses', sampledRequestsEnabled: true },
      });
    }

    const webAcl = new wafv2.CfnWebACL(this, 'WebAcl', {
      defaultAction: hasIpRestriction ? { block: {} } : { allow: {} },
      scope: 'CLOUDFRONT',
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: `ai-persona-waf-${envName}`,
        sampledRequestsEnabled: true,
      },
      name: `ai-persona-waf-${envName}`,
      rules: [
        ...ipRules,
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
