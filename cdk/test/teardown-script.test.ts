import * as fs from 'fs';
import * as path from 'path';
import { synthBoth } from './helpers/stages';

/**
 * 環境削除のスクリプトが RETAIN のリソースを漏れなく扱うことの検査。
 *
 * destroy.sh は、スタックを消しても残るリソースを種類（RETAINED_TYPES）で拾って一覧にし、
 * scripts/purge-retained.sh は種類ごとの手順（purge_resource の case）で削除する。CDK に RETAIN の
 * 種類が増えたのにスクリプトが追いつかないと、黙って残り続けるため、合成したテンプレートと
 * 突き合わせる。
 */

const ROOT = path.join(__dirname, '..', '..');
const SCRIPT = fs.readFileSync(path.join(ROOT, 'destroy.sh'), 'utf8');
const PURGE_SCRIPT = fs.readFileSync(path.join(ROOT, 'scripts', 'purge-retained.sh'), 'utf8');

function retainedTypesInScript(): Set<string> {
  const line = SCRIPT.split('\n').find((l) => l.startsWith('RETAINED_TYPES='));
  if (!line) {
    throw new Error('RETAINED_TYPES is not defined in destroy.sh');
  }
  return new Set([...line.matchAll(/'(AWS::[A-Za-z0-9]+::[A-Za-z0-9]+)'/g)].map((m) => m[1]));
}

function purgeHandlersInScript(): Set<string> {
  const start = PURGE_SCRIPT.indexOf('purge_resource() {');
  if (start < 0) {
    throw new Error('purge_resource is not defined in scripts/purge-retained.sh');
  }
  const body = PURGE_SCRIPT.slice(start);
  return new Set([...body.matchAll(/^\s+(AWS::[A-Za-z0-9]+::[A-Za-z0-9]+)\)/gm)].map((m) => m[1]));
}

function retainedTypesInStages(): Set<string> {
  const { publicStage, closedStage } = synthBoth();
  const types = new Set<string>();
  for (const stage of [publicStage, closedStage]) {
    for (const template of Object.values(stage.templates)) {
      const resources = template.toJSON().Resources as Record<string, { Type: string; DeletionPolicy?: string }>;
      for (const resource of Object.values(resources)) {
        if (resource.DeletionPolicy === 'Retain') {
          types.add(resource.Type);
        }
      }
    }
  }
  return types;
}

describe('the teardown scripts and the retained resources of the v1 Stages', () => {
  const stageTypes = retainedTypesInStages();

  test('the Stages have retained resources to compare against', () => {
    expect(stageTypes.size).toBeGreaterThan(0);
  });

  test('every retained resource type is listed in RETAINED_TYPES', () => {
    const listed = retainedTypesInScript();
    const missing = [...stageTypes].filter((type) => !listed.has(type));
    expect(missing).toEqual([]);
  });

  test('every type in RETAINED_TYPES has a purge_resource handler', () => {
    const handlers = purgeHandlersInScript();
    const missing = [...retainedTypesInScript()].filter((type) => !handlers.has(type));
    expect(missing).toEqual([]);
  });
});
