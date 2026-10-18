import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AIPersonaStage } from '../lib/ai-persona-stage';
import { createDevStage } from '../lib/dev-stage';
import { devParameter } from '../parameters';

/**
 * Stage AIPersonaDev とコンテキストの検査。FR1.9、FR9.1、FR9.2、ADR-001、NFR9.3、
 * Infrastructure Design レビュー R-09（imageTag なしでも synth は通す）。
 * U9: bin/app.ts とテストが同じ組み立て（createDevStage）を通る（NFR7.15）。
 */

// Stage を 2 通り synth するテストがあるため、既定の 5 秒では足りない
jest.setTimeout(60_000);

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

describe('createDevStage（U9、bin/app.ts と同じ組み立て）', () => {
  test('今の bin/app.ts と同じ Stage AIPersonaDev の 3 スタックを作り、template も同じ', () => {
    const viaFunction = createDevStage(new cdk.App(), { account: ACCOUNT });
    const direct = new AIPersonaStage(new cdk.App(), 'AIPersonaDev', {
      account: ACCOUNT,
      parameter: devParameter,
    });
    expect(viaFunction.node.id).toBe('AIPersonaDev');
    const a = viaFunction.synth();
    const b = direct.synth();
    expect(a.stacks.map((s) => s.stackName).sort()).toEqual([
      'AIPersonaDev-App',
      'AIPersonaDev-Data',
      'AIPersonaDev-Edge',
    ]);
    for (const name of ['AIPersonaDev-App', 'AIPersonaDev-Data', 'AIPersonaDev-Edge']) {
      expect(a.getStackByName(name).template).toEqual(b.getStackByName(name).template);
    }
  });

  test('overrides の enableAdditionalPersonaModels と enableWaf が効き、devParameter は書き換えない', () => {
    const s = createDevStage(new cdk.App(), {
      account: ACCOUNT,
      enableAdditionalPersonaModels: true,
      enableWaf: true,
    });
    Template.fromStack(s.edge).resourceCountIs('AWS::WAFv2::WebACL', 1);
    const taskDef = Object.values(
      Template.fromStack(s.app).findResources('AWS::ECS::TaskDefinition'),
    )[0];
    const env = Object.fromEntries(
      (taskDef.Properties.ContainerDefinitions[0].Environment as { Name: string; Value: any }[]).map(
        (e) => [e.Name, e.Value],
      ),
    );
    expect(env.ENABLE_ADDITIONAL_PERSONA_MODELS).toBe('true');
    expect(devParameter.enableWaf).toBe(false);
    expect(devParameter.enableAdditionalPersonaModels).toBe(false);

    const defaults = createDevStage(new cdk.App(), { account: ACCOUNT });
    Template.fromStack(defaults.edge).resourceCountIs('AWS::WAFv2::WebACL', 0);
  });
});

function stackDesired(s: AIPersonaStage): number | undefined {
  const template = s.synth().getStackByName('AIPersonaDev-App').template;
  const service = Object.values(template.Resources as Record<string, any>).find(
    (r) => r.Type === 'AWS::ECS::Service',
  );
  return service?.Properties?.DesiredCount;
}
