import { Construct } from 'constructs';
import { CfnWaitConditionHandle, Stack, StackProps } from 'aws-cdk-lib';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';

/**
 * Rate limit for login-related URLs. Paths are checked against the app routes
 * (web/routers/auth.py) and tests/infra/test_waf_login_paths.py. If legitimate users get
 * 403 (for example an organization sharing one egress IP), revisit the threshold (cdk/README.md, operations for admins).
 */
export const LOGIN_RATE_LIMITED_PATHS = ['/login', '/auth/callback', '/auth/new-password'] as const;
/** Number of requests allowed per IP per evaluation window (seconds). */
export const LOGIN_RATE_LIMIT = 100;
export const LOGIN_RATE_WINDOW_SEC = 300;

export interface EdgeStackProps extends StackProps {
  envName: string;
  /** Whether to create the WAF for CloudFront (enableWaf in parameters.ts). */
  enableWaf: boolean;
  /** Allowed IPs (CIDR). When set, all others are blocked. */
  allowedIpAddresses?: string[];
}

/** visibilityConfig with metrics and sampled requests enabled. */
function visibility(metricName: string): wafv2.CfnWebACL.VisibilityConfigProperty {
  return { cloudWatchMetricsEnabled: true, metricName, sampledRequestsEnabled: true };
}

/**
 * Rate limit for login-related URLs (priority 0).
 *
 * Placed before the IP allow rule because allow ends evaluation. Blocked requests get the WAF default
 * 403 and never reach the app. The method is ignored (GET pages, POST submits and
 * returns from Cognito are all counted).
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
                // The app (uvicorn) decodes the path once before routing (/%6cogin is
                // /login). WAF also decodes once before comparing, so another spelling cannot evade the limit
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
 * Rules carried over from v0: IP allow (priority 1 if set), common rules (2), and the rate
 * limit for all URLs (3).
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
          // SizeRestrictions_BODY (8KB body limit) is incompatible with file uploads (10MB/file),
          // so downgrade it to Count. Size validation is done in the app
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
 * Edge stack: WAF WebACL for CloudFront (us-east-1).
 *
 * Carries over the v0 WafStack contents (IP restriction, common rules, global rate limit) and adds
 * the rate limit for login-related URLs at the front. The WebACL is created only when enableWaf: true (default)
 * (with false, only a NoWafPlaceholder is placed). The WebACL ARN is passed to the App stack via
 * crossRegionReferences.
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
      // A CloudFormation stack cannot be empty, so place a no-op marker
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
