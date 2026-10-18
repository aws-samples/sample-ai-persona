import { App, Aspects, Stage, Validations } from 'aws-cdk-lib';
import { AwsSolutionsChecks, WriteNagSuppressionsToCloudFormationAspect } from 'cdk-nag';
import { AppParameter, devParameter } from '../parameters';
import { AIPersonaStage } from './ai-persona-stage';

/** Stage AIPersonaDev の組み立てで上書きできる値（テストで 2 通りの構成を作る）。 */
export interface DevStageOverrides {
  /** デプロイ先のアカウント。省略時は devParameter.env.account（CDK_DEFAULT_ACCOUNT）。 */
  account?: string;
  enableAdditionalPersonaModels?: AppParameter['enableAdditionalPersonaModels'];
  enableWaf?: AppParameter['enableWaf'];
}

/**
 * Stage AIPersonaDev（Data・App・Edge）を作る（U9、NFR7.15）。
 *
 * bin/app.ts とテストが同じ組み立てを通るように lib/ に置く。devParameter は書き換えず、
 * overrides を重ねた写しを渡す。
 */
export function createDevStage(app: App, overrides: DevStageOverrides = {}): AIPersonaStage {
  const { account, enableAdditionalPersonaModels, enableWaf } = overrides;
  const parameter: AppParameter = {
    ...devParameter,
    ...(enableAdditionalPersonaModels !== undefined ? { enableAdditionalPersonaModels } : {}),
    ...(enableWaf !== undefined ? { enableWaf } : {}),
  };
  return new AIPersonaStage(app, 'AIPersonaDev', { parameter, account });
}

/**
 * cdk-nag の AwsSolutions を App 全体（Stage の中のスタックを含む）に掛ける（NFR7.15、Q1 A）。
 *
 * 呼ぶのは bin/app.ts と test/cdk-nag.test.ts だけで、Stage を作った後に呼ぶ。抑止
 * （acknowledge）はリソースのそばに置き、template の Metadata（cdk_nag.rules_to_suppress）
 * にも書く（監査のため）。writeSuppressionsToCloudFormation が App に足す Aspect は Stage の
 * 境界を越えないため、同じ Aspect を各 Stage にも足す。
 */
export function applyNagChecks(app: App): void {
  const stages = app.node.children.filter((child): child is Stage => Stage.isStage(child));
  if (stages.length === 0) {
    throw new Error('applyNagChecks() must be called after the Stage is created (createDevStage).');
  }
  Validations.of(app).addPlugins(
    new AwsSolutionsChecks(app, { writeSuppressionsToCloudFormation: true }),
  );
  for (const stage of stages) {
    Aspects.of(stage).add(new WriteNagSuppressionsToCloudFormationAspect());
  }
}
