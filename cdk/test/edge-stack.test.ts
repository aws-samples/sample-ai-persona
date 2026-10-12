import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { EdgeStack } from '../lib/edge-stack';

/**
 * Edge スタック（CloudFront 用 WAF、us-east-1）の検査。FR1.8（NFR4.4 のレート制限は U9）。
 */

const ENV = { account: '123456789012', region: 'us-east-1' };

function synth(props: { enableWaf: boolean; allowedIpAddresses?: string[] }): EdgeStack {
  const app = new cdk.App();
  return new EdgeStack(app, 'Edge', { env: ENV, envName: 'dev', ...props });
}

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

test('enableWaf: false なら WebACL を作らない（スタックは空にならない）', () => {
  const stack = synth({ enableWaf: false });
  const template = Template.fromStack(stack);
  template.resourceCountIs('AWS::WAFv2::WebACL', 0);
  expect(stack.webAclArn).toBeUndefined();
  expect(Object.keys(template.toJSON().Resources ?? {}).length).toBeGreaterThan(0);
});

test('許可する IP があるのに WAF が無効なら synth でエラー', () => {
  expect(() => synth({ enableWaf: false, allowedIpAddresses: ['203.0.113.0/24'] })).toThrow(
    /enableWaf/,
  );
});
