/**
 * 包含の判定（テスト用の補助、閉域対応 U2 BR1.4）。
 *
 * ロールの文の (Action, Resource) が、エンドポイントのポリシーの Allow の文に含まれるかを判定する。
 * - ワイルドカードは IAM と同じ解釈: `*` はスラッシュ・コロンを含む任意の文字列（空も含む）、`?` は 1 文字
 * - ロール側の `*`・`?` はそれ自体が範囲を表すため、エンドポイント側のより広いワイルドカードにだけ含まれる
 * - Action は大文字・小文字を区別しない（IAM と同じ）
 * - CloudFormation の参照（Ref・Fn::GetAtt・Fn::ImportValue・Fn::Join）は flattenPolicyValue で 1 つの
 *   まとまりの文字列にし、同じ式どうしを同じ値として比べる（参照の中の文字はワイルドカードに使わない）
 *
 * テンプレートの値は手で書き写さず、合成したテンプレートの文をそのまま渡す（unit-test-instructions.md）。
 */

/** One IAM policy statement as it appears in a synthesized template. */
export interface TemplateStatement {
  Effect?: string;
  Action?: unknown;
  Resource?: unknown;
  [key: string]: unknown;
}

/** Opening and closing marks of a flattened reference (a single unit; never part of a wildcard). */
const REF_OPEN = '\u0001';
const REF_CLOSE = '\u0002';

/**
 * Flatten a template value to one string. References become `\u0001<kind>:<target>\u0002`, so the
 * same expression flattens to the same string and differs from every other reference.
 */
export function flattenPolicyValue(value: unknown): string {
  if (typeof value === 'string') {
    return value;
  }
  const node = (value ?? {}) as Record<string, unknown>;
  if (Array.isArray(node['Fn::Join'])) {
    const [delimiter, items] = node['Fn::Join'] as [string, unknown[]];
    return items.map(flattenPolicyValue).join(delimiter);
  }
  for (const kind of ['Ref', 'Fn::GetAtt', 'Fn::ImportValue']) {
    if (node[kind] !== undefined) {
      const target = Array.isArray(node[kind]) ? (node[kind] as unknown[]).join('.') : JSON.stringify(node[kind]);
      return `${REF_OPEN}${kind}:${target}${REF_CLOSE}`;
    }
  }
  return `${REF_OPEN}${JSON.stringify(value)}${REF_CLOSE}`;
}

/** Split into tokens: a whole reference, `*`, `?`, or one literal character. */
function tokenize(text: string): string[] {
  const tokens: string[] = [];
  for (let i = 0; i < text.length; i++) {
    if (text[i] === REF_OPEN) {
      const end = text.indexOf(REF_CLOSE, i);
      tokens.push(text.slice(i, end + 1));
      i = end;
    } else {
      tokens.push(text[i]);
    }
  }
  return tokens;
}

/**
 * Whether every value `value` stands for is matched by `pattern` (IAM wildcards on both sides).
 *
 * A pattern `*` consumes any run of value tokens (including value wildcards); a pattern `?`
 * consumes one token that is not a value `*`; literals and references must be equal.
 */
export function patternCovers(pattern: string, value: string): boolean {
  const p = tokenize(pattern);
  const v = tokenize(value);
  // reachable[j]: p[0..i) can cover v[0..j)
  let reachable = new Array<boolean>(v.length + 1).fill(false);
  reachable[0] = true;
  for (const token of p) {
    const next = new Array<boolean>(v.length + 1).fill(false);
    if (token === '*') {
      let seen = false;
      for (let j = 0; j <= v.length; j++) {
        seen = seen || reachable[j];
        next[j] = seen;
      }
    } else {
      for (let j = 1; j <= v.length; j++) {
        const item = v[j - 1];
        const matches = token === '?' ? item !== '*' : token === item;
        next[j] = reachable[j - 1] && matches;
      }
    }
    reachable = next;
  }
  return reachable[v.length];
}

const asList = (value: unknown): unknown[] => (value === undefined ? [] : ([] as unknown[]).concat(value));

/** Whether an Allow statement of `statements` allows `action` on `resource`. */
export function statementsAllow(statements: readonly TemplateStatement[], action: string, resource: unknown): boolean {
  const target = flattenPolicyValue(resource);
  return statements.some(
    (statement) =>
      statement.Effect === 'Allow' &&
      asList(statement.Action).some((allowed) =>
        patternCovers(String(allowed).toLowerCase(), action.toLowerCase()),
      ) &&
      asList(statement.Resource).some((allowed) => patternCovers(flattenPolicyValue(allowed), target)),
  );
}

/** Every (action, resource) pair of a role statement, flattened for messages. */
export function grantsOf(statement: TemplateStatement): { action: string; resource: unknown }[] {
  return asList(statement.Action).flatMap((action) =>
    asList(statement.Resource).map((resource) => ({ action: String(action), resource })),
  );
}

/** A readable form of a flattened value (for failure messages). */
export function describePolicyValue(value: unknown): string {
  return flattenPolicyValue(value).split(REF_OPEN).join('{').split(REF_CLOSE).join('}');
}
