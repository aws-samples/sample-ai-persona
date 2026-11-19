import { Construct } from 'constructs';
import { Stack } from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import { CodeInterpreterExecutionRole } from '../deployment-profile';

export interface AnalysisCodeInterpreterProps {
  /** Stage の環境の名前（Code Interpreter の名前に入れ、アカウントの中で重ならないようにする）。 */
  envName: string;
  /** 実行ロールの付け方（deployment-profile.ts の CODE_INTERPRETER_EXECUTION_ROLE）。 */
  executionRole: CodeInterpreterExecutionRole;
}

/**
 * 高度な生成（ベータ、U1）の前準備と分析のセッションを開く Code Interpreter（App スタック、
 * 公開の Stage だけ。infrastructure-specification.md、NFR3.4）。
 *
 * - ネットワークは SANDBOX（VPC に置かない）。持ち込みは署名付き URL
 * - 実行ロールは既定で付けない（`none`）。切り替え先の `empty` は許可のポリシーを持たないロール
 * - 呼べるのはタスクロールだけ（App スタックがこの ARN の 3 つの操作だけを付ける）
 * - 状態を持たない（セッションは使い捨て）ため、App スタックの中で作り直してよい
 */
export class AnalysisCodeInterpreter extends Construct {
  public readonly resource: agentcore.CfnCodeInterpreterCustom;
  /** `empty` のときだけある（許可のポリシーなし）。 */
  public readonly executionRole?: iam.Role;

  constructor(scope: Construct, id: string, props: AnalysisCodeInterpreterProps) {
    super(scope, id);

    if (props.executionRole === 'empty') {
      // 信頼は AgentCore のサービスだけで、このアカウントからの呼び出しに限る（混乱した代理の防止）
      this.executionRole = new iam.Role(this, 'ExecutionRole', {
        description: 'AI Persona advanced generation Code Interpreter (no permissions)',
        assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
          conditions: { StringEquals: { 'aws:SourceAccount': Stack.of(this).account } },
        }),
      });
    }

    this.resource = new agentcore.CfnCodeInterpreterCustom(this, 'Resource', {
      // AgentCore の名前は英字で始まる英数字と _ の 48 文字まで
      name: `AIPersona_v1_${props.envName.replace(/[^A-Za-z0-9]/g, '_')}_analysis`.slice(0, 48),
      description: 'AI Persona advanced generation (beta): data preparation and analysis sessions',
      networkConfiguration: { networkMode: 'SANDBOX' },
      ...(this.executionRole ? { executionRoleArn: this.executionRole.roleArn } : {}),
    });
  }

  /** The ID the app passes to StartCodeInterpreterSession (CODE_INTERPRETER_ID). */
  public get codeInterpreterId(): string {
    return this.resource.attrCodeInterpreterId;
  }

  /** The ARN the task role may call (no wildcard). */
  public get codeInterpreterArn(): string {
    return this.resource.attrCodeInterpreterArn;
  }
}
