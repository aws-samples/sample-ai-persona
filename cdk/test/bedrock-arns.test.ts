import * as cdk from 'aws-cdk-lib';
import { bedrockInferenceArns, inferenceArnList, surveyModelArns } from '../lib/bedrock-arns';
import { PUBLIC_PROFILE } from '../lib/deployment-profile';

/**
 * BedrockArns（閉域対応 U5、BR2.1〜BR2.3）の単体テスト。
 *
 * inferenceArnList() は CDK の構成部品に依存しない純粋な関数。範囲・Stack のリージョン・
 * 明示したモデル ID から、推論で呼んでよい ARN の一覧を作る。
 */

const ENV = { partition: 'aws', account: '123456789012', region: 'ap-northeast-1' };
const GLOBAL_ARNS = [
  'arn:aws:bedrock:*::foundation-model/*',
  'arn:aws:bedrock:*:123456789012:inference-profile/*',
  'arn:aws:bedrock:*:123456789012:application-inference-profile/*',
];
const JP_ARNS = [
  'arn:aws:bedrock:ap-northeast-1::foundation-model/*',
  'arn:aws:bedrock:ap-northeast-3::foundation-model/*',
  'arn:aws:bedrock:ap-northeast-1:123456789012:inference-profile/jp.*',
  'arn:aws:bedrock:*:123456789012:application-inference-profile/*',
];

describe('inferenceArnList: global（BR2.1）', () => {
  test('今の 3 つの ARN', () => {
    expect(inferenceArnList(ENV, { inferenceScope: 'global', explicitModelIds: {} })).toEqual(GLOBAL_ARNS);
  });

  test('明示したモデル ID があっても足さない（3 つに含まれる）', () => {
    expect(
      inferenceArnList(ENV, {
        inferenceScope: 'global',
        explicitModelIds: { agentModelId: 'anthropic.claude-haiku-4-5-20251001-v1:0' },
      }),
    ).toEqual(GLOBAL_ARNS);
  });
});

describe('inferenceArnList: jp（BR2.2、BR2.3）', () => {
  test('国内の 2 つのリージョンの基盤モデル、Stack のリージョンの jp.*、アプリケーション推論プロファイル', () => {
    expect(inferenceArnList(ENV, { inferenceScope: 'jp', explicitModelIds: {} })).toEqual(JP_ARNS);
  });

  test('大阪の Stack なら推論プロファイルは ap-northeast-3 の jp.*', () => {
    const arns = inferenceArnList({ ...ENV, region: 'ap-northeast-3' }, { inferenceScope: 'jp', explicitModelIds: {} });
    expect(arns).toContain('arn:aws:bedrock:ap-northeast-3:123456789012:inference-profile/jp.*');
  });

  test('jp.* の明示は足さない', () => {
    expect(
      inferenceArnList(ENV, {
        inferenceScope: 'jp',
        explicitModelIds: { agentModelId: 'jp.anthropic.claude-haiku-4-5-20251001-v1:0' },
      }),
    ).toEqual(JP_ARNS);
  });

  test.each([
    ['global.anthropic.claude-sonnet-5', 'anthropic.claude-sonnet-5'],
    ['us.anthropic.claude-haiku-4-5-20251001-v1:0', 'anthropic.claude-haiku-4-5-20251001-v1:0'],
    ['us-gov.anthropic.claude-x', 'anthropic.claude-x'],
    ['apac.amazon.nova-pro-v1:0', 'amazon.nova-pro-v1:0'],
  ])('推論プロファイルの ID %p は、そのプロファイルとリージョン * の基盤モデルを足す', (id, model) => {
    const arns = inferenceArnList(ENV, { inferenceScope: 'jp', explicitModelIds: { bedrockModelId: id } });
    expect(arns).toEqual([
      ...JP_ARNS,
      `arn:aws:bedrock:*:123456789012:inference-profile/${id}`,
      `arn:aws:bedrock:*::foundation-model/${model}`,
    ]);
  });

  test('接頭辞のない基盤モデルの ID は Stack のリージョンの基盤モデルを足す', () => {
    const arns = inferenceArnList(ENV, {
      inferenceScope: 'jp',
      explicitModelIds: { agentModelId: 'anthropic.claude-haiku-4-5-20251001-v1:0' },
    });
    expect(arns).toEqual([
      ...JP_ARNS,
      'arn:aws:bedrock:ap-northeast-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0',
    ]);
  });

  test('同じ ARN は 1 つにまとめる', () => {
    const arns = inferenceArnList(ENV, {
      inferenceScope: 'jp',
      explicitModelIds: {
        bedrockModelId: 'global.anthropic.claude-sonnet-5',
        agentModelId: 'us.anthropic.claude-sonnet-5',
      },
    });
    expect(arns).toEqual([
      ...JP_ARNS,
      'arn:aws:bedrock:*:123456789012:inference-profile/global.anthropic.claude-sonnet-5',
      'arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5',
      'arn:aws:bedrock:*:123456789012:inference-profile/us.anthropic.claude-sonnet-5',
    ]);
    expect(new Set(arns).size).toBe(arns.length);
  });

  test('ARN の形はパラメータの名前と値を示してエラー', () => {
    const arn = 'arn:aws:bedrock:ap-northeast-1:123456789012:inference-profile/jp.anthropic.claude-haiku-5-5';
    expect(() =>
      inferenceArnList(ENV, { inferenceScope: 'jp', explicitModelIds: { bedrockModelId: arn } }),
    ).toThrow(/bedrockModelId.*arn:aws:bedrock.*ID で渡す/);
  });
});

describe('surveyModelArns: アンケートのステートマシンが呼ぶモデル（基盤設計 3.1、NFR5.3）', () => {
  test('global はこのアカウントの推論プロファイルと、リージョン * の基盤モデルの 2 つ', () => {
    expect(surveyModelArns(ENV, 'global.anthropic.claude-haiku-5-5')).toEqual([
      'arn:aws:bedrock:*:123456789012:inference-profile/global.anthropic.claude-haiku-5-5',
      'arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5',
    ]);
  });

  test('jp は Stack のリージョンの推論プロファイルと、国内 2 リージョンの基盤モデル', () => {
    expect(surveyModelArns(ENV, 'jp.anthropic.claude-haiku-5-5')).toEqual([
      'arn:aws:bedrock:ap-northeast-1:123456789012:inference-profile/jp.anthropic.claude-haiku-5-5',
      'arn:aws:bedrock:ap-northeast-1::foundation-model/anthropic.claude-haiku-5-5',
      'arn:aws:bedrock:ap-northeast-3::foundation-model/anthropic.claude-haiku-5-5',
    ]);
  });

  test.each([
    'us.anthropic.claude-haiku-5-5',
    'anthropic.claude-haiku-5-5',
    'arn:aws:bedrock:ap-northeast-1:123456789012:inference-profile/jp.anthropic.claude-haiku-5-5',
    '',
  ])('global・jp 以外の %p は名前を示してエラー（今より広げない）', (value) => {
    expect(() => surveyModelArns(ENV, value)).toThrow(/surveyModelId/);
  });

  test('アンケートのモデルはタスクロールの推論の一覧に足さない（呼ぶのはステートマシン）', () => {
    expect(
      inferenceArnList(ENV, {
        inferenceScope: 'jp',
        explicitModelIds: { surveyModelId: 'global.anthropic.claude-haiku-5-5' },
      }),
    ).toEqual(JP_ARNS);
  });
});

describe('bedrockInferenceArns: Stack のパーティション・アカウント・リージョンで作る', () => {
  test('profile を省くと公開（global）の 3 つ', () => {
    const stack = new cdk.Stack(new cdk.App(), 'S', { env: { account: ENV.account, region: ENV.region } });
    const resolved = bedrockInferenceArns(stack).map((arn) => stack.resolve(arn));
    expect(resolved).toHaveLength(3);
    expect(bedrockInferenceArns(stack, PUBLIC_PROFILE)).toHaveLength(3);
  });

  test('jp の profile なら国内の 4 つ', () => {
    const stack = new cdk.Stack(new cdk.App(), 'S', { env: { account: ENV.account, region: ENV.region } });
    expect(bedrockInferenceArns(stack, { inferenceScope: 'jp', explicitModelIds: {} })).toHaveLength(4);
  });
});
