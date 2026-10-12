import { Construct } from 'constructs';
import { CfnWaitConditionHandle, Stack, StackProps } from 'aws-cdk-lib';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';

export interface EdgeStackProps extends StackProps {
  envName: string;
  /** CloudFront 用の WAF を作るか（parameters.ts の enableWaf）。 */
  enableWaf: boolean;
  /** 許可する IP（CIDR）。指定すると、それ以外を拒否する。 */
  allowedIpAddresses?: string[];
}

/**
 * Edge スタック: CloudFront 用の WAF WebACL（us-east-1、FR1.8）。
 *
 * v0 の WafStack の中身（IP 制限、共通ルール、全体のレート制限）を移した。
 * ログイン系の URL のレート制限は U9 で足す。App スタックへは WebACL の ARN を
 * crossRegionReferences で渡す（約束 C10）。
 */
export class EdgeStack extends Stack {
  public readonly webAclArn?: string;

  constructor(scope: Construct, id: string, props: EdgeStackProps) {
    super(scope, id, props);

    const { envName, enableWaf, allowedIpAddresses } = props;
    const hasIpRestriction = (allowedIpAddresses?.length ?? 0) > 0;

    if (!enableWaf && hasIpRestriction) {
      throw new Error(
        'allowedIpAddresses is set but enableWaf is false. Enable WAF or remove the IP list.',
      );
    }
    if (!enableWaf) {
      // リソースのない CloudFormation スタックは作れないため、何もしない印を置く
      new CfnWaitConditionHandle(this, 'NoWafPlaceholder');
      return;
    }

    const ipRules: wafv2.CfnWebACL.RuleProperty[] = [];
    if (hasIpRestriction) {
      const ipSet = new wafv2.CfnIPSet(this, 'AllowedIpSet', {
        name: `ai-persona-v1-allowed-ips-${envName}`,
        scope: 'CLOUDFRONT',
        ipAddressVersion: 'IPV4',
        addresses: allowedIpAddresses ?? [],
      });
      ipRules.push({
        name: 'AllowedIpAddresses',
        priority: 0,
        action: { allow: {} },
        statement: { ipSetReferenceStatement: { arn: ipSet.attrArn } },
        visibilityConfig: {
          cloudWatchMetricsEnabled: true,
          metricName: 'AllowedIpAddresses',
          sampledRequestsEnabled: true,
        },
      });
    }

    const webAcl = new wafv2.CfnWebACL(this, 'WebAcl', {
      name: `ai-persona-v1-waf-${envName}`,
      scope: 'CLOUDFRONT',
      defaultAction: hasIpRestriction ? { block: {} } : { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: `ai-persona-v1-waf-${envName}`,
        sampledRequestsEnabled: true,
      },
      rules: [
        ...ipRules,
        {
          name: 'AWSManagedRulesCommonRuleSet',
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesCommonRuleSet',
              // SizeRestrictions_BODY（ボディ 8KB 制限）はファイルアップロード（10MB/ファイル）と
              // 両立しないため Count に落とす。サイズ検証はアプリ側で行う
              ruleActionOverrides: [{ name: 'SizeRestrictions_BODY', actionToUse: { count: {} } }],
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'AWSManagedRulesCommonRuleSet',
            sampledRequestsEnabled: true,
          },
        },
        {
          name: 'RateLimit',
          priority: 2,
          action: { block: {} },
          statement: { rateBasedStatement: { limit: 2000, aggregateKeyType: 'IP' } },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'RateLimit',
            sampledRequestsEnabled: true,
          },
        },
      ],
    });
    this.webAclArn = webAcl.attrArn;
  }
}
