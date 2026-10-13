import * as cdk from 'aws-cdk-lib';
import { AIPersonaStage } from '../lib/ai-persona-stage';
import { devParameter } from '../parameters';

/**
 * Stage AIPersonaDev とコンテキストの検査。FR1.9、FR9.1、FR9.2、ADR-001、NFR9.3、
 * Infrastructure Design レビュー R-09（imageTag なしでも synth は通す）。
 */

const ACCOUNT = '123456789012';

function stage(context: Record<string, string> = {}): AIPersonaStage {
  const app = new cdk.App({ context });
  return new AIPersonaStage(app, 'AIPersonaDev', { account: ACCOUNT, parameter: devParameter });
}

test('Data・App・Edge の 3 つのスタックを持つ', () => {
  const s = stage();
  expect([s.data, s.app, s.edge].map((x) => x.node.id).sort()).toEqual(['App', 'Data', 'Edge']);
  expect(s.data.region).toBe('ap-northeast-1');
  expect(s.app.region).toBe('ap-northeast-1');
  expect(s.edge.region).toBe('us-east-1');
});

test('-c region で Data・App のリージョンを変えられる（deploy.sh --region）。Edge は us-east-1 のまま', () => {
  const s = stage({ region: 'us-east-1' });
  expect(s.data.region).toBe('us-east-1');
  expect(s.app.region).toBe('us-east-1');
  expect(s.edge.region).toBe('us-east-1');
});

test('CloudFront のプレフィックスリストが分からないリージョンは synth でエラー', () => {
  expect(() => stage({ region: 'us-west-2' })).toThrow(/prefix list/);
});

test('依存は App → Data と App → Edge だけ', () => {
  const s = stage();
  const ids = (stack: cdk.Stack) => stack.dependencies.map((d) => d.node.id).sort();
  expect(ids(s.app)).toEqual(['Data', 'Edge']);
  expect(ids(s.data)).toEqual([]);
  expect(ids(s.edge)).toEqual([]);
});

test('imageTag がなくても synth が通る', () => {
  const assembly = stage().synth();
  expect(assembly.stacks.map((st) => st.stackName).sort()).toEqual([
    'AIPersonaDev-App',
    'AIPersonaDev-Data',
    'AIPersonaDev-Edge',
  ]);
});

test('deploymentMode=closed は synth でエラー', () => {
  expect(() => stage({ deploymentMode: 'closed' })).toThrow('閉域構成は後続リリースで提供');
});

test('deploymentMode が未知ならエラー', () => {
  expect(() => stage({ deploymentMode: 'private' })).toThrow(/deploymentMode/);
});

test('enableMcpGateway=true は synth でエラー', () => {
  expect(() => stage({ enableMcpGateway: 'true' })).toThrow(/MCP Gateway/);
});

test.each(['0', '11', '1.5', 'two'])('appDesiredCount=%s は範囲外としてエラー', (count) => {
  expect(() => stage({ appDesiredCount: count })).toThrow(/appDesiredCount/);
});

test('appDesiredCount はコンテキストで 1〜10 に変えられる（既定 1）', () => {
  expect(stackDesired(stage())).toBe(1);
  expect(stackDesired(stage({ appDesiredCount: '3' }))).toBe(3);
});

test('v0 のスタック名・エクスポート名（AIPersona-dev-*）と重ならない', () => {
  const assembly = stage({ imageTag: 'abc1234' }).synth();
  for (const artifact of assembly.stacks) {
    expect(artifact.stackName).not.toMatch(/^AIPersona(Ecr|Cognito|Waf|Memory|Mcp)?-dev$/);
    const outputs = (artifact.template.Outputs ?? {}) as Record<string, { Export?: { Name: string } }>;
    for (const output of Object.values(outputs)) {
      const name = JSON.stringify(output.Export?.Name ?? '');
      expect(name).not.toMatch(/AIPersona(Ecr|Cognito|Waf|Memory|Mcp)?-dev-/);
    }
  }
});

function stackDesired(s: AIPersonaStage): number | undefined {
  const template = s.synth().getStackByName('AIPersonaDev-App').template;
  const service = Object.values(template.Resources as Record<string, any>).find(
    (r) => r.Type === 'AWS::ECS::Service',
  );
  return service?.Properties?.DesiredCount;
}
