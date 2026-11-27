import { App, Aspects, Stage, Validations } from 'aws-cdk-lib';
import { AwsSolutionsChecks, WriteNagSuppressionsToCloudFormationAspect } from 'cdk-nag';
import { AppParameter, closedDevParameter, devParameter } from '../parameters';
import { AIPersonaStage } from './ai-persona-stage';
import { closedStageRequested, mergeClosedStageContext } from './deployment-profile';

/** ID of the closed Stage. It sits alongside the public AIPersonaDev. */
export const CLOSED_DEV_STAGE_ID = 'AIPersonaDevClosed';

/** Values that can be overridden when building the closed Stage. */
export interface ClosedDevStageOverrides {
  /** Target account. Defaults to closedDevParameter.env.account (CDK_DEFAULT_ACCOUNT). */
  account?: string;
}

/** Values that can be overridden when building Stage AIPersonaDev (tests build two configurations). */
export interface DevStageOverrides {
  /** Target account. Defaults to devParameter.env.account (CDK_DEFAULT_ACCOUNT). */
  account?: string;
  enableAdditionalPersonaModels?: AppParameter['enableAdditionalPersonaModels'];
  enableWaf?: AppParameter['enableWaf'];
}

/**
 * Creates Stage AIPersonaDev (Data, App, Edge).
 *
 * Lives in lib/ so bin/app.ts and the tests share the same assembly. devParameter is not mutated;
 * a copy with overrides layered on top is passed.
 */
export function createDevStage(app: App, overrides: DevStageOverrides = {}): AIPersonaStage {
  const { account, enableAdditionalPersonaModels, enableWaf } = overrides;
  const parameter: AppParameter = {
    ...devParameter,
    ...(enableAdditionalPersonaModels !== undefined ? { enableAdditionalPersonaModels } : {}),
    ...(enableWaf !== undefined ? { enableWaf } : {}),
  };
  return new AIPersonaStage(app, 'AIPersonaDev', { parameter, account, deploymentMode: 'public' });
}

/**
 * Creates the closed Stage AIPersonaDevClosed (Data, App) only when closed parameters are present.
 *
 * Layers the context values closedCertificateArn, closedDomainName, closedAllowedCidrs,
 * closedCreateTestEnvironment and closedVpcCidr onto the closed defaults of closedDevParameter.
 * Returns undefined without doing anything unless both the certificate ARN and FQDN are given (so CI synth and
 * deploy.sh without --closed behave as before). Giving only one is an error. bin/app.ts calls it before applyNagChecks.
 */
export function createClosedDevStage(
  app: App,
  overrides: ClosedDevStageOverrides = {},
): AIPersonaStage | undefined {
  const base = closedDevParameter.closed;
  if (!base) {
    throw new Error('closedDevParameter has no closed-network defaults (parameters.ts).');
  }
  const closed = mergeClosedStageContext(base, (key) => app.node.tryGetContext(key));
  if (!closedStageRequested(closed)) {
    return undefined;
  }
  const parameter: AppParameter = { ...closedDevParameter, closed };
  return new AIPersonaStage(app, CLOSED_DEV_STAGE_ID, {
    parameter,
    account: overrides.account,
    deploymentMode: 'closed',
  });
}

/**
 * Applies cdk-nag AwsSolutions to the whole App (including stacks inside Stages).
 *
 * Called only from bin/app.ts and test/cdk-nag.test.ts, after the Stage is created. Suppressions
 * (acknowledge) sit next to the resources and are also written to the template Metadata (cdk_nag.rules_to_suppress)
 * for audit. The Aspect that writeSuppressionsToCloudFormation adds to the App does not cross the Stage
 * boundary, so the same Aspect is added to each Stage as well.
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
