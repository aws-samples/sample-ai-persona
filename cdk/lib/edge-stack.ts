import { Construct } from 'constructs';
import { CfnWaitConditionHandle, Stack, StackProps } from 'aws-cdk-lib';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';

/**
 * ログイン系の URL のレート制限（U9、NFR4.16、約束 C11）。パスはアプリのルート
 * （web/routers/auth.py）と tests/unit/test_waf_login_paths.py で照らす。正当な利用者が
 * 403 になるとき（出口の IP を多くの人が共有する組織など）は閾値を見直す（cdk/README.md「運用（管理者向け）」）。
 */
export const LOGIN_RATE_LIMITED_PATHS = ['/login', '/auth/callback', '/auth/new-password'] as const;
/** 評価の窓（秒）ごとに IP 1 つから許す要求の数。 */
export const LOGIN_RATE_LIMIT = 100;
export const LOGIN_RATE_WINDOW_SEC = 300;

export interface EdgeStackProps extends StackProps {
  envName: string;
  /** CloudFront 用の WAF を作るか（parameters.ts の enableWaf）。 */
  enableWaf: boolean;
  /** 許可する IP（CIDR）。指定すると、それ以外を拒否する。 */
  allowedIpAddresses?: string[];
}

/** メトリクスとサンプルの要求を有効にした visibilityConfig（NFR8.27）。 */
function visibility(metricName: string): wafv2.CfnWebACL.VisibilityConfigProperty {
  return { cloudWatchMetricsEnabled: true, metricName, sampledRequestsEnabled: true };
}

/**
 * ログイン系の URL のレート制限（priority 0、NFR4.16〜NFR4.18）。
 *
 * IP 許可のルールは allow で評価を打ち切るため、その前に置く（NFR4.17）。止めた要求は
 * WAF の既定の 403 を返し、アプリには届かない。メソッドは見ない（GET の画面、POST の送信、
 * Cognito からの戻りのすべてを数える）。
 */
function loginRateLimitRule(): wafv2.CfnWebACL.RuleProperty {
  return {
    name: 'LoginRateLimit',
    priority: 0,
    action: { block: {} },
    statement: {
      rateBasedStatement: {
        limit: LOGIN_RATE_LIMIT,
        evaluationWindowSec: LOGIN_RATE_WINDOW_SEC,
        aggregateKeyType: 'IP',
        scopeDownStatement: {
          orStatement: {
            statements: LOGIN_RATE_LIMITED_PATHS.map((path) => ({
              byteMatchStatement: {
                fieldToMatch: { uriPath: {} },
                positionalConstraint: 'EXACTLY',
                searchString: path,
                // アプリ（uvicorn）はパスのエンコードを 1 回戻してからルートを選ぶ（/%6cogin は
                // /login）。WAF も比較の前に 1 回戻し、別の表記で制限を外せないようにする
                textTransformations: [{ priority: 0, type: 'URL_DECODE' }],
              },
            })),
          },
        },
      },
    },
    visibilityConfig: visibility('LoginRateLimit'),
  };
}

/**
 * v0 から移したルール（U1）: IP 許可（あれば priority 1）、共通ルール（2）、全 URL の
 * レート制限（3）。
 */
function baseRules(ipSetArn?: string): wafv2.CfnWebACL.RuleProperty[] {
  const ipRules: wafv2.CfnWebACL.RuleProperty[] = ipSetArn
    ? [
        {
          name: 'AllowedIpAddresses',
          priority: 1,
          action: { allow: {} },
          statement: { ipSetReferenceStatement: { arn: ipSetArn } },
          visibilityConfig: visibility('AllowedIpAddresses'),
        },
      ]
    : [];
  return [
    ...ipRules,
    {
      name: 'AWSManagedRulesCommonRuleSet',
      priority: 2,
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
      visibilityConfig: visibility('AWSManagedRulesCommonRuleSet'),
    },
    {
      name: 'RateLimit',
      priority: 3,
      action: { block: {} },
      statement: { rateBasedStatement: { limit: 2000, aggregateKeyType: 'IP' } },
      visibilityConfig: visibility('RateLimit'),
    },
  ];
}

/**
 * Edge スタック: CloudFront 用の WAF WebACL（us-east-1、FR1.8）。
 *
 * v0 の WafStack の中身（IP 制限、共通ルール、全体のレート制限）を移した。U9 で
 * ログイン系の URL のレート制限を先頭に足した。WebACL は enableWaf: true のときだけ作る
 * （利用者の指示、2026-10-17。false では NoWafPlaceholder だけを置く）。App スタックへは
 * WebACL の ARN を crossRegionReferences で渡す（約束 C10）。
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

    const ipSet = hasIpRestriction
      ? new wafv2.CfnIPSet(this, 'AllowedIpSet', {
          name: `ai-persona-v1-allowed-ips-${envName}`,
          scope: 'CLOUDFRONT',
          ipAddressVersion: 'IPV4',
          addresses: allowedIpAddresses ?? [],
        })
      : undefined;

    const webAcl = new wafv2.CfnWebACL(this, 'WebAcl', {
      name: `ai-persona-v1-waf-${envName}`,
      scope: 'CLOUDFRONT',
      defaultAction: hasIpRestriction ? { block: {} } : { allow: {} },
      visibilityConfig: visibility(`ai-persona-v1-waf-${envName}`),
      rules: [loginRateLimitRule(), ...baseRules(ipSet?.attrArn)],
    });
    this.webAclArn = webAcl.attrArn;
  }
}
