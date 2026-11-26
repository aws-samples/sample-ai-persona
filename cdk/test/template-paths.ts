import * as cdk from 'aws-cdk-lib';

/**
 * Tools that avoid writing CDK-generated names (logical ID hash suffixes, Exports output names) in tests.
 *
 * Resources are identified by construct path (the `aws:cdk:path` metadata), and Data stack
 * output names by the destination resource path plus attribute (`{AIPersonaDev/Data/UploadBucket/Bucket/Resource.Arn}`).
 * Paths consist only of construct IDs decided in code, so they are safe to keep in OSS sources.
 */

/** Context that makes synth write `aws:cdk:path` into every resource's Metadata. */
export const PATH_METADATA_CONTEXT = { [cdk.cx_api.PATH_METADATA_ENABLE_CONTEXT]: true };

/** The construct path recorded in a template resource's Metadata. */
export function pathOf(resource: { Metadata?: Record<string, unknown> }): string {
  const value = resource.Metadata?.['aws:cdk:path'];
  if (typeof value !== 'string') {
    throw new Error('The resource has no aws:cdk:path metadata; synth with PATH_METADATA_CONTEXT.');
  }
  return value;
}

/** `{<construct path>.<attribute>}`: how a cross-stack reference is written in expectations. */
export function exportedRef(path: string, attribute: string): string {
  return `{${path}.${attribute}}`;
}

/**
 * Map every export name of a template to `{<path of the exported resource>.<attribute>}`.
 *
 * Only outputs whose value is a plain `Fn::GetAtt` are mapped (the shape of every automatic
 * cross-stack reference in this app); anything else is left out and stays visible as-is.
 */
export function exportTargets(template: { Resources?: any; Outputs?: any }): Map<string, string> {
  const resources = (template.Resources ?? {}) as Record<string, any>;
  const targets = new Map<string, string>();
  for (const output of Object.values((template.Outputs ?? {}) as Record<string, any>)) {
    const name = output.Export?.Name;
    const getAtt = output.Value?.['Fn::GetAtt'];
    if (typeof name !== 'string' || !Array.isArray(getAtt)) {
      continue;
    }
    const [logicalId, attribute] = getAtt as [string, string];
    targets.set(name, exportedRef(pathOf(resources[logicalId]), attribute));
  }
  return targets;
}

/**
 * Flatten a policy resource (`Fn::Join` / `Fn::ImportValue` / `Ref` / `Fn::GetAtt`) to one
 * string, writing imports as `{path.attribute}` (unknown exports keep their name).
 */
export function flattenReference(value: unknown, targets: Map<string, string>): string {
  if (typeof value === 'string') {
    return value;
  }
  const node = (value ?? {}) as Record<string, any>;
  if (Array.isArray(node['Fn::Join'])) {
    const [delimiter, items] = node['Fn::Join'] as [string, unknown[]];
    return items.map((item) => flattenReference(item, targets)).join(delimiter);
  }
  if (typeof node['Fn::ImportValue'] === 'string') {
    return targets.get(node['Fn::ImportValue']) ?? node['Fn::ImportValue'];
  }
  if (typeof node.Ref === 'string') {
    return `<${node.Ref}>`;
  }
  if (Array.isArray(node['Fn::GetAtt'])) {
    return `<${(node['Fn::GetAtt'] as string[]).join('.')}>`;
  }
  return JSON.stringify(value);
}

/** Replace each export name in `text` with its `{path.attribute}` form. */
export function describeExports(text: string, targets: Map<string, string>): string {
  let described = text;
  for (const [name, ref] of targets) {
    described = described.split(name).join(ref);
  }
  return described;
}
