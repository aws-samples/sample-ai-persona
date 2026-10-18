import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { createDevStage } from '../lib/dev-stage';
import {
  EdgeStack,
  LOGIN_RATE_LIMIT,
  LOGIN_RATE_LIMITED_PATHS,
  LOGIN_RATE_WINDOW_SEC,
} from '../lib/edge-stack';

/**
 * Edge スタック（CloudFront 用 WAF、us-east-1）の検査。FR1.8、NFR4.15〜NFR4.18、NFR8.27。
 *
 * U9: ログイン系の URL のレート制限（LoginRateLimit）は enableWaf: true のときに作る WebACL に
 * 足す。enableWaf: false では WebACL を作らない（利用者の指示で設計から変えた、2026-10-17）。
 */

jest.setTimeout(60_000);

const ENV = { account: '123456789012', region: 'us-east-1' };

function synth(props: { enableWaf: boolean; allowedIpAddresses?: string[] }): EdgeStack {
  const app = new cdk.App();
  return new EdgeStack(app, 'Edge', { env: ENV, envName: 'dev', ...props });
}

function rules(stack: EdgeStack): any[] {
  const acls = Template.fromStack(stack).findResources('AWS::WAFv2::WebACL');
  expect(Object.keys(acls)).toEqual(['WebAcl']);
  return acls.WebAcl.Properties.Rules;
}

const ruleNamed = (all: any[], name: string) => {
  const found = all.filter((r) => r.Name === name);
  expect(found).toHaveLength(1);
  return found[0];
};

test('us-east-1 に置く', () => {
  expect(synth({ enableWaf: true }).region).toBe('us-east-1');
});

test('enableWaf: true なら CLOUDFRONT スコープの WebACL と共通ルール', () => {
  const stack = synth({ enableWaf: true });
  const template = Template.fromStack(stack);
  template.hasResourceProperties('AWS::WAFv2::WebACL', {
    Scope: 'CLOUDFRONT',
    DefaultAction: { Allow: {} },
    Rules: Match.arrayWith([
      Match.objectLike({ Name: 'AWSManagedRulesCommonRuleSet' }),
    ]),
  });
  expect(stack.webAclArn).toBeDefined();
});

test('許可する IP があれば IP セットで許可し、既定は拒否', () => {
  const template = Template.fromStack(
    synth({ enableWaf: true, allowedIpAddresses: ['203.0.113.0/24'] }),
  );
  template.hasResourceProperties('AWS::WAFv2::IPSet', {
    Scope: 'CLOUDFRONT',
    Addresses: ['203.0.113.0/24'],
  });
  template.hasResourceProperties('AWS::WAFv2::WebACL', {
    DefaultAction: { Block: {} },
    Rules: Match.arrayWith([Match.objectLike({ Name: 'AllowedIpAddresses' })]),
  });
});

test('enableWaf: false なら WebACL を作らず、NoWafPlaceholder を置く（スタックは空にならない）', () => {
  const stack = synth({ enableWaf: false });
  const template = Template.fromStack(stack);
  template.resourceCountIs('AWS::WAFv2::WebACL', 0);
  expect(stack.webAclArn).toBeUndefined();
  expect(Object.keys(template.findResources('AWS::CloudFormation::WaitConditionHandle'))).toEqual([
    'NoWafPlaceholder',
  ]);
});

test('許可する IP があるのに WAF が無効なら synth でエラー', () => {
  expect(() => synth({ enableWaf: false, allowedIpAddresses: ['203.0.113.0/24'] })).toThrow(
    /enableWaf/,
  );
});

describe('ログイン系の URL のレート制限 LoginRateLimit（U9、NFR4.16、NFR4.17、NFR8.27）', () => {
  test('閾値とパスの定数: 300 秒で 100 件、/login・/auth/callback・/auth/new-password', () => {
    expect(LOGIN_RATE_LIMIT).toBe(100);
    expect(LOGIN_RATE_WINDOW_SEC).toBe(300);
    expect([...LOGIN_RATE_LIMITED_PATHS]).toEqual(['/login', '/auth/callback', '/auth/new-password']);
  });

  test('priority 0 で block、IP ごとに 300 秒 100 件、3 つのパスの完全一致の OR（メソッドは見ない）', () => {
    const rule = ruleNamed(rules(synth({ enableWaf: true })), 'LoginRateLimit');
    expect(rule.Priority).toBe(0);
    expect(rule.Action).toEqual({ Block: {} });
    const rate = rule.Statement.RateBasedStatement;
    expect(rate.Limit).toBe(LOGIN_RATE_LIMIT);
    expect(rate.EvaluationWindowSec).toBe(LOGIN_RATE_WINDOW_SEC);
    expect(rate.AggregateKeyType).toBe('IP');
    const matches = rate.ScopeDownStatement.OrStatement.Statements;
    expect(matches).toEqual(
      LOGIN_RATE_LIMITED_PATHS.map((path) => ({
        ByteMatchStatement: {
          FieldToMatch: { UriPath: {} },
          PositionalConstraint: 'EXACTLY',
          SearchString: path,
          TextTransformations: [{ Priority: 0, Type: 'NONE' }],
        },
      })),
    );
  });

  test('メトリクスとサンプルの要求を有効にし、メトリクス名は LoginRateLimit（アラームは作らない）', () => {
    const stack = synth({ enableWaf: true });
    const rule = ruleNamed(rules(stack), 'LoginRateLimit');
    expect(rule.VisibilityConfig).toEqual({
      CloudWatchMetricsEnabled: true,
      SampledRequestsEnabled: true,
      MetricName: 'LoginRateLimit',
    });
    Template.fromStack(stack).resourceCountIs('AWS::CloudWatch::Alarm', 0);
  });

  test('IP 許可・共通ルール・全 URL のレート制限は priority 1・2・3（ログイン系を先に評価する）', () => {
    const all = rules(synth({ enableWaf: true, allowedIpAddresses: ['203.0.113.0/24'] }));
    const priorities = Object.fromEntries(all.map((r) => [r.Name, r.Priority]));
    expect(priorities).toEqual({
      LoginRateLimit: 0,
      AllowedIpAddresses: 1,
      AWSManagedRulesCommonRuleSet: 2,
      RateLimit: 3,
    });
    // IP 制限がなくても、ほかのルールの priority は変わらない
    const withoutIp = rules(synth({ enableWaf: true }));
    expect(Object.fromEntries(withoutIp.map((r) => [r.Name, r.Priority]))).toEqual({
      LoginRateLimit: 0,
      AWSManagedRulesCommonRuleSet: 2,
      RateLimit: 3,
    });
  });
});

describe('CloudFront への WebACL の付け外し（Stage、NFR4.15 は enableWaf: true のときだけ）', () => {
  const distributionConfig = (enableWaf: boolean) => {
    const stage = createDevStage(new cdk.App(), { account: '123456789012', enableWaf });
    const distributions = Template.fromStack(stage.app).findResources('AWS::CloudFront::Distribution');
    return Object.values(distributions)[0].Properties.DistributionConfig;
  };

  test('enableWaf: true なら CloudFront の WebACLId に Edge の WebACL を渡す', () => {
    expect(JSON.stringify(distributionConfig(true).WebACLId)).toContain('WebAcl');
  });

  test('enableWaf: false なら CloudFront に WebACLId がない', () => {
    expect(distributionConfig(false).WebACLId).toBeUndefined();
  });
});
