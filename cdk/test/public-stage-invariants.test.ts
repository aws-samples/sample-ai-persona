import { Template } from 'aws-cdk-lib/assertions';
import { synthPublic } from './helpers/stages';

/**
 * 公開の Stage AIPersonaDev の「写し」をスナップショットで固定する（U1 閉域、NFR7.1、
 * security-design.md §5、BR2.2）。
 *
 * 閉域構成の変更の前のコードで作ったスナップショット
 * （__snapshots__/public-stage-invariants.test.ts.snap）と比べる。落ちたら、閉域の変更が
 * 公開構成を変えてしまったということなので、スナップショットを更新せずに原因を直す。
 * イメージのタグ・アセットのハッシュ・アカウント（テストの固定値）など変わりやすい値は写さない。
 */

jest.setTimeout(120_000);

const { templates } = synthPublic();

/** `Ref` / `Fn::GetAtt` / `Fn::ImportValue` / `Fn::Join` を、参照先の名前だけの文字列にする。 */
function simplify(value: unknown): unknown {
  if (value === null || typeof value !== 'object') {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map(simplify);
  }
  const node = value as Record<string, any>;
  if (typeof node.Ref === 'string') {
    return `Ref(${node.Ref})`;
  }
  if (Array.isArray(node['Fn::GetAtt'])) {
    return `GetAtt(${(node['Fn::GetAtt'] as string[]).join('.')})`;
  }
  if (node['Fn::ImportValue'] !== undefined) {
    return `Import(${String(simplify(node['Fn::ImportValue']))})`;
  }
  if (Array.isArray(node['Fn::Join'])) {
    const [delimiter, items] = node['Fn::Join'] as [string, unknown[]];
    return items.map((item) => {
      const s = simplify(item);
      return typeof s === 'string' ? s : JSON.stringify(s);
    }).join(delimiter);
  }
  return Object.fromEntries(Object.entries(node).map(([k, v]) => [k, simplify(v)]));
}

function resourceTypeCounts(template: Template): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const resource of Object.values(template.toJSON().Resources as Record<string, any>)) {
    counts[resource.Type] = (counts[resource.Type] ?? 0) + 1;
  }
  return Object.fromEntries(Object.entries(counts).sort(([a], [b]) => a.localeCompare(b)));
}

/** 1 つのルールを「相手・プロトコル・ポート」の文字列にする。 */
function ruleOf(rule: Record<string, any>): string {
  const peer =
    rule.CidrIp ??
    rule.CidrIpv6 ??
    rule.SourcePrefixListId ??
    rule.DestinationPrefixListId ??
    rule.SourceSecurityGroupId ??
    rule.DestinationSecurityGroupId;
  return `${JSON.stringify(simplify(peer))} ${rule.IpProtocol} ${rule.FromPort ?? ''}-${rule.ToPort ?? ''}`;
}

/** SG ごとの受け入れ・外向きのルール（SG の中に書くものと、別のリソースのものの両方）。 */
function securityGroupRules(template: Template): Record<string, { ingress: string[]; egress: string[] }> {
  const resources = template.toJSON().Resources as Record<string, any>;
  const groups: Record<string, { ingress: string[]; egress: string[] }> = {};
  for (const [id, resource] of Object.entries(resources)) {
    if (resource.Type !== 'AWS::EC2::SecurityGroup') {
      continue;
    }
    groups[id] = {
      ingress: (resource.Properties.SecurityGroupIngress ?? []).map(ruleOf),
      egress: (resource.Properties.SecurityGroupEgress ?? []).map(ruleOf),
    };
  }
  for (const [type, direction] of [
    ['AWS::EC2::SecurityGroupIngress', 'ingress'],
    ['AWS::EC2::SecurityGroupEgress', 'egress'],
  ] as const) {
    for (const resource of Object.values(resources)) {
      if (resource.Type !== type) {
        continue;
      }
      const groupId = resource.Properties.GroupId;
      const group = Array.isArray(groupId?.['Fn::GetAtt'])
        ? String(groupId['Fn::GetAtt'][0])
        : String(simplify(groupId));
      groups[group] ??= { ingress: [], egress: [] };
      groups[group][direction].push(ruleOf(resource.Properties));
    }
  }
  for (const group of Object.values(groups)) {
    group.ingress.sort();
    group.egress.sort();
  }
  return Object.fromEntries(Object.entries(groups).sort(([a], [b]) => a.localeCompare(b)));
}

function taskEnvironment(template: Template): Record<string, unknown> {
  const definitions = Object.values(template.findResources('AWS::ECS::TaskDefinition'));
  expect(definitions).toHaveLength(1);
  const environment = definitions[0].Properties.ContainerDefinitions[0].Environment as {
    Name: string;
    Value: unknown;
  }[];
  return Object.fromEntries(
    environment
      .map((e) => [e.Name, simplify(e.Value)] as const)
      .sort(([a], [b]) => a.localeCompare(b)),
  );
}

function taskRoleStatements(template: Template): unknown[] {
  const roles = Object.keys(template.findResources('AWS::IAM::Role')).filter((id) =>
    id.startsWith('AppTaskDefinitionTaskRole'),
  );
  expect(roles).toHaveLength(1);
  const policies = Object.values(template.findResources('AWS::IAM::Policy')).filter((policy) =>
    (policy.Properties.Roles as any[]).some((r) => r.Ref === roles[0]),
  );
  expect(policies.length).toBeGreaterThan(0);
  return policies.flatMap((policy) => simplify(policy.Properties.PolicyDocument.Statement) as unknown[]);
}

function retainedLogicalIds(template: Template): string[] {
  return Object.entries(template.toJSON().Resources as Record<string, any>)
    .filter(([, resource]) => resource.DeletionPolicy === 'Retain')
    .map(([id, resource]) => `${id} ${resource.Type}`)
    .sort();
}

describe('公開の Stage AIPersonaDev の写し（変更の前に固定、NFR7.1）', () => {
  test('スタックは Data・App・Edge の 3 つ', () => {
    expect(Object.keys(templates).sort()).toEqual(['App', 'Data', 'Edge']);
  });

  test.each(['Data', 'App', 'Edge'])('%s スタックのリソースの型ごとの数', (stack) => {
    expect(resourceTypeCounts(templates[stack])).toMatchSnapshot();
  });

  test('App スタックの SG のルール', () => {
    expect(securityGroupRules(templates.App)).toMatchSnapshot();
  });

  test('タスクの環境変数（キーで並べる）', () => {
    expect(taskEnvironment(templates.App)).toMatchSnapshot();
  });

  test('タスクロールの IAM の文', () => {
    expect(taskRoleStatements(templates.App)).toMatchSnapshot();
  });

  test('Data スタックのステートフルな資源（Retain）の論理 ID', () => {
    expect(retainedLogicalIds(templates.Data)).toMatchSnapshot();
  });
});
