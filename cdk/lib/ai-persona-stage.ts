import { Construct } from 'constructs';
import { Stage, StageProps } from 'aws-cdk-lib';
import { AppParameter, DeploymentMode } from '../parameters';
import { AppStack } from './app-stack';
import { DataStack } from './data-stack';
import { resolveDeploymentProfile, ResolvedProfile } from './deployment-profile';
import { EdgeStack } from './edge-stack';

export interface AIPersonaStageProps extends StageProps {
  parameter: AppParameter;
  /** デプロイ先のアカウント。省略時は parameter.env.account。 */
  account?: string;
  /**
   * この Stage のモード（既定 public）。Stage を作る側（createDevStage / createClosedDevStage）が
   * 決め、Stage はコンテキストの deploymentMode ではなくこの値で組み立てを変える（U1 閉域、BR1.2）。
   */
  deploymentMode?: DeploymentMode;
}

const MIN_DESIRED_COUNT = 1;
const MAX_DESIRED_COUNT = 10;
const EDGE_REGION = 'us-east-1';

/**
 * v1.0.0 の Stage（FR9.1、ADR-001）。
 *
 * - 公開（deploymentMode=public、AIPersonaDev）: Data・App・Edge の 3 スタック。依存は App → Data と
 *   App → Edge だけ
 * - 閉域（deploymentMode=closed、AIPersonaDevClosed。U1 閉域、BR2.1）: Data・App の 2 スタック。
 *   CloudFront と WAF を使わないため Edge を作らず、App は Data にだけ依存する
 *
 * 設定は parameters.ts を既定にし、コンテキスト（-c region=... / -c appDesiredCount=... /
 * -c enableMcpGateway=...）で上書きできる。モードの解決と閉域のパラメータの検査は
 * DeploymentProfile（deployment-profile.ts）が行い、synth 時に決められない形（MCP Gateway・
 * 台数の範囲外・閉域のパラメータの誤り）は synth でエラーにする。公開の Stage に
 * -c deploymentMode=closed を渡すとエラー（閉域は別の Stage で作る）。v0 のスタックとは
 * 名前・エクスポート名が重ならない。
 */
export class AIPersonaStage extends Stage {
  public readonly profile: ResolvedProfile;
  public readonly data: DataStack;
  /** 公開の Stage だけにある（閉域は Edge を作らない）。 */
  public readonly edge?: EdgeStack;
  public readonly app: AppStack;

  constructor(scope: Construct, id: string, props: AIPersonaStageProps) {
    super(scope, id, props);

    const { parameter } = props;
    const account = props.account ?? parameter.env?.account;
    // deploy.sh --region は -c region=<リージョン> で渡す（bootstrap・出力の取得と同じリージョンにする）
    const region =
      (this.node.tryGetContext('region') as string | undefined) ??
      parameter.env?.region ??
      'ap-northeast-1';

    this.profile = resolveDeploymentProfile({
      deploymentMode: this.resolveDeploymentMode(props, parameter),
      region,
      closed: parameter.closed,
      enableMcpGateway: this.mcpGatewayEnabled(parameter),
      // U3: the closed profile rejects these two (BR5.4, BR5.5); the public one ignores them
      enableAdditionalPersonaModels: parameter.enableAdditionalPersonaModels,
      dataAgentRuntimeArn: parameter.dataAgentRuntimeArn,
      // U5: 推論の範囲はコンテキスト（deploy.sh --inference-scope）が parameters.ts より優先（BR1.1）
      inferenceScope: this.resolveInferenceScope(parameter),
      bedrockModelId: parameter.bedrockModelId,
      agentModelId: parameter.agentModelId,
      batchInferenceModelId: parameter.batchInferenceModelId,
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
        enableWaf: parameter.enableWaf ?? false,
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
   * `-c deploymentMode=closed` (BR1.2) and passes any other context value on to the profile,
   * which rejects values other than public and closed (BR1.4). The closed Stage does not
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

  /** `-c inferenceScope=...`, then `parameter.inferenceScope` (the profile checks the value). */
  private resolveInferenceScope(parameter: AppParameter): string | undefined {
    const fromContext = this.node.tryGetContext('inferenceScope');
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
