import { Construct } from 'constructs';
import { Stage, StageProps } from 'aws-cdk-lib';
import { AppParameter, DeploymentMode } from '../parameters';
import { AppStack } from './app-stack';
import { DataStack } from './data-stack';
import { EdgeStack } from './edge-stack';

export interface AIPersonaStageProps extends StageProps {
  parameter: AppParameter;
  /** デプロイ先のアカウント。省略時は parameter.env.account。 */
  account?: string;
}

const MIN_DESIRED_COUNT = 1;
const MAX_DESIRED_COUNT = 10;
const EDGE_REGION = 'us-east-1';

/**
 * v1.0.0 の Stage（FR9.1、ADR-001）: Data・App・Edge の 3 スタック。
 *
 * 依存は App → Data と App → Edge だけ。設定は parameters.ts を既定にし、
 * コンテキスト（-c deploymentMode=... / -c appDesiredCount=... / -c enableMcpGateway=...）
 * で上書きできる。synth 時に決められない形（閉域構成・MCP Gateway・台数の範囲外）は
 * synth でエラーにする。v0 のスタックとは名前・エクスポート名が重ならない。
 */
export class AIPersonaStage extends Stage {
  public readonly data: DataStack;
  public readonly edge: EdgeStack;
  public readonly app: AppStack;

  constructor(scope: Construct, id: string, props: AIPersonaStageProps) {
    super(scope, id, props);

    const { parameter } = props;
    this.checkDeploymentMode(parameter);
    this.checkMcpGateway(parameter);
    const appDesiredCount = this.resolveDesiredCount(parameter);

    const account = props.account ?? parameter.env?.account;
    const region = parameter.env?.region ?? 'ap-northeast-1';

    this.data = new DataStack(this, 'Data', {
      env: { account, region },
      parameter,
      description: `AI Persona v1 data (stateful, RETAIN) - ${parameter.envName}`,
    });
    this.edge = new EdgeStack(this, 'Edge', {
      env: { account, region: EDGE_REGION },
      envName: parameter.envName,
      enableWaf: parameter.enableWaf ?? false,
      allowedIpAddresses: parameter.allowedIpAddresses,
      crossRegionReferences: true,
      description: `AI Persona v1 CloudFront WAF (${EDGE_REGION}) - ${parameter.envName}`,
    });
    this.app = new AppStack(this, 'App', {
      env: { account, region },
      parameter,
      data: this.data,
      webAclArn: this.edge.webAclArn,
      appDesiredCount,
      crossRegionReferences: true,
      description: `AI Persona v1 app (Fargate, ALB, CloudFront) - ${parameter.envName}`,
    });
    this.app.addStackDependency(this.data, 'App reads tables, keys and the user pool from Data');
    this.app.addStackDependency(this.edge, 'App attaches the CloudFront WebACL from Edge');
  }

  private checkDeploymentMode(parameter: AppParameter): void {
    const mode = (this.node.tryGetContext('deploymentMode') ??
      parameter.deploymentMode ??
      'public') as DeploymentMode | string;
    if (mode === 'closed') {
      throw new Error(
        '閉域構成は後続リリースで提供（deploymentMode=closed は v1.0.0 では使えません）',
      );
    }
    if (mode !== 'public') {
      throw new Error(`deploymentMode must be "public" or "closed", got "${mode}"`);
    }
  }

  private checkMcpGateway(parameter: AppParameter): void {
    const fromContext = this.node.tryGetContext('enableMcpGateway');
    const enabled =
      fromContext === undefined ? parameter.enableMcpGateway === true : String(fromContext) === 'true';
    if (enabled) {
      throw new Error('MCP Gateway は後続リリースで提供（enableMcpGateway=true は v1.0.0 では使えません）');
    }
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
