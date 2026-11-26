/**
 * Coverage check (a test helper).
 *
 * Decides whether a role statement's (Action, Resource) is covered by an Allow statement of an endpoint policy.
 * - Wildcards are read as IAM does: `*` matches any string including slashes and colons (and empty), `?` one character
 * - `*` and `?` on the role side express a range themselves, so they are covered only by a broader wildcard on the endpoint side
 * - Action is case-insensitive (as in IAM)
 * - CloudFormation references (Ref, Fn::GetAtt, Fn::ImportValue, Fn::Join) are turned into one unit string
 *   by flattenPolicyValue and compared as the same value when the expressions are the same (characters inside a reference are not used as wildcards)
 *
 * Template values are not copied by hand; the statements of the synthesized template are passed as is.
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
