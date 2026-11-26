import { Construct } from 'constructs';
import { Stage, StageProps } from 'aws-cdk-lib';
import { AppParameter, DeploymentMode } from '../parameters';
import { AppStack } from './app-stack';
import { DataStack } from './data-stack';
import { resolveDeploymentProfile, ResolvedProfile } from './deployment-profile';
import { EdgeStack } from './edge-stack';

export interface AIPersonaStageProps extends StageProps {
  parameter: AppParameter;
  /** Target account. Defaults to parameter.env.account. */
  account?: string;
  /**
   * Mode of this Stage (default public). The caller (createDevStage / createClosedDevStage)
   * decides it, and the Stage builds from this value rather than the deploymentMode context.
   */
  deploymentMode?: DeploymentMode;
}

const MIN_DESIRED_COUNT = 1;
const MAX_DESIRED_COUNT = 10;
const EDGE_REGION = 'us-east-1';
/** The closed Stage's inference scope context key (deploy.sh --closed --inference-scope). */
const CLOSED_INFERENCE_SCOPE_CONTEXT = 'closedInferenceScope';

/**
 * The v1.0.0 Stage.
 *
 * - Public (deploymentMode=public, AIPersonaDev): three stacks, Data, App and Edge. The only
 *   dependencies are App -> Data and App -> Edge.
 * - Closed (deploymentMode=closed, AIPersonaDevClosed): two stacks, Data and App. It uses no
 *   CloudFront or WAF, so there is no Edge and App depends on Data only.
 *
 * Settings default to parameters.ts and can be overridden by context (-c region=... /
 * -c appDesiredCount=... / -c enableMcpGateway=...). DeploymentProfile (deployment-profile.ts)
 * resolves the mode and validates closed parameters; values that cannot be decided at synth time
 * (MCP Gateway, out-of-range counts, invalid closed parameters) fail synth. Passing
 * -c deploymentMode=closed to the public Stage is an error (the closed one is a separate Stage).
 * Names and export names do not overlap with the v0 stacks.
 */
export class AIPersonaStage extends Stage {
  public readonly profile: ResolvedProfile;
  public readonly data: DataStack;
  /** Only in the public Stage (the closed Stage has no Edge). */
  public readonly edge?: EdgeStack;
  public readonly app: AppStack;

  constructor(scope: Construct, id: string, props: AIPersonaStageProps) {
    super(scope, id, props);

    const { parameter } = props;
    const account = props.account ?? parameter.env?.account;
    // deploy.sh --region is passed as -c region=<region> (same region as bootstrap and output lookup)
    const region =
      (this.node.tryGetContext('region') as string | undefined) ??
      parameter.env?.region ??
      'ap-northeast-1';

    this.profile = resolveDeploymentProfile({
      deploymentMode: this.resolveDeploymentMode(props, parameter),
      region,
      closed: parameter.closed,
      enableMcpGateway: this.mcpGatewayEnabled(parameter),
      // The closed profile rejects these two; the public one ignores them
      enableAdditionalPersonaModels: parameter.enableAdditionalPersonaModels,
      dataAgentRuntimeArn: parameter.dataAgentRuntimeArn,
      // The Stage context (deploy.sh --inference-scope) takes precedence over parameters.ts for the inference scope
      inferenceScope: this.resolveInferenceScope(props, parameter),
      bedrockModelId: parameter.bedrockModelId,
      agentModelId: parameter.agentModelId,
      surveyModelId: parameter.surveyModelId,
    });
    const appDesiredCount = this.resolveDesiredCount(parameter);
    const closed = this.profile.deploymentMode === 'closed';

    this.data = new DataStack(this, 'Data', {
      env: { account, region },
      parameter,
      createCognitoDomain: this.profile.createCognitoDomain,
      description: `AI Persona v1 data (stateful, RETAIN) - ${parameter.envName}`,
    });
    if (!closed) {
      this.edge = new EdgeStack(this, 'Edge', {
        env: { account, region: EDGE_REGION },
        envName: parameter.envName,
        enableWaf: parameter.enableWaf ?? true,
        allowedIpAddresses: parameter.allowedIpAddresses,
        crossRegionReferences: true,
        description: `AI Persona v1 CloudFront WAF (${EDGE_REGION}) - ${parameter.envName}`,
      });
    }
    this.app = new AppStack(this, 'App', {
      env: { account, region },
      parameter,
      profile: this.profile,
      data: this.data,
      webAclArn: this.edge?.webAclArn,
      appDesiredCount,
      ...(closed ? {} : { crossRegionReferences: true }),
      description: closed
        ? `AI Persona v1 app (Fargate, internal HTTPS ALB, closed network) - ${parameter.envName}`
        : `AI Persona v1 app (Fargate, ALB, CloudFront) - ${parameter.envName}`,
    });
    this.app.addStackDependency(this.data, 'App reads tables, keys and the user pool from Data');
    if (this.edge) {
      this.app.addStackDependency(this.edge, 'App attaches the CloudFront WebACL from Edge');
    }
  }

  /**
   * The Stage's mode: the value the Stage was built with. The public Stage rejects
   * `-c deploymentMode=closed` and passes any other context value on to the profile,
   * which rejects values other than public and closed. The closed Stage does not
   * read the context.
   */
  private resolveDeploymentMode(props: AIPersonaStageProps, parameter: AppParameter): string {
    if (props.deploymentMode === 'closed') {
      return 'closed';
    }
    const mode = String(this.node.tryGetContext('deploymentMode') ?? parameter.deploymentMode ?? 'public');
    if (mode === 'closed') {
      throw new Error(
        '閉域構成は別の Stage で作る（公開の Stage に deploymentMode=closed は渡せません。' +
          '閉域のパラメータ -c closedCertificateArn=... -c closedDomainName=... を渡すと ' +
          'AIPersonaDevClosed を作ります）',
      );
    }
    return mode;
  }

  /**
   * The Stage's own context key (`-c inferenceScope=...` for the public Stage,
   * `-c closedInferenceScope=...` for the closed Stage), then `parameter.inferenceScope`
   * (the profile checks the value). deploy.sh --closed synthesizes the public Stage too, so a
   * shared key would apply the closed Stage's scope to the public Stage as well.
   */
  private resolveInferenceScope(props: AIPersonaStageProps, parameter: AppParameter): string | undefined {
    const key = props.deploymentMode === 'closed' ? CLOSED_INFERENCE_SCOPE_CONTEXT : 'inferenceScope';
    const fromContext = this.node.tryGetContext(key);
    return fromContext === undefined || fromContext === null ? parameter.inferenceScope : String(fromContext);
  }

  private mcpGatewayEnabled(parameter: AppParameter): boolean {
    const fromContext = this.node.tryGetContext('enableMcpGateway');
    return fromContext === undefined ? parameter.enableMcpGateway === true : String(fromContext) === 'true';
  }

  private resolveDesiredCount(parameter: AppParameter): number {
    const raw = this.node.tryGetContext('appDesiredCount') ?? parameter.appDesiredCount ?? 1;
    const count = Number(raw);
    if (!Number.isInteger(count) || count < MIN_DESIRED_COUNT || count > MAX_DESIRED_COUNT) {
      throw new Error(
        `appDesiredCount must be an integer from ${MIN_DESIRED_COUNT} to ${MAX_DESIRED_COUNT}, got "${raw}"`,
      );
    }
    return count;
  }
}
