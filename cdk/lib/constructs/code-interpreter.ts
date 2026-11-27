import { Construct } from 'constructs';
import { Stack } from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import { CodeInterpreterExecutionRole } from '../deployment-profile';

export interface AnalysisCodeInterpreterProps {
  /** Stage environment name (part of the Code Interpreter name, to keep it unique in the account). */
  envName: string;
  /** How the execution role is attached (CODE_INTERPRETER_EXECUTION_ROLE in deployment-profile.ts). */
  executionRole: CodeInterpreterExecutionRole;
}

/**
 * Code Interpreter that runs the preparation and analysis sessions of advanced generation (beta)
 * (App stack, public Stage only).
 *
 * - Network mode is SANDBOX (not placed in the VPC); data comes in via presigned URLs
 * - No execution role by default (`none`); the alternative `empty` is a role with no permission policies
 * - Only the task role can call it (the App stack grants exactly three actions on this ARN)
 * - Stateless (sessions are disposable), so it can be recreated within the App stack
 */
export class AnalysisCodeInterpreter extends Construct {
  public readonly resource: agentcore.CfnCodeInterpreterCustom;
  /** Present only for `empty` (no permission policies). */
  public readonly executionRole?: iam.Role;

  constructor(scope: Construct, id: string, props: AnalysisCodeInterpreterProps) {
    super(scope, id);

    if (props.executionRole === 'empty') {
      // Trust is limited to the AgentCore service and calls from this account (confused deputy protection)
      this.executionRole = new iam.Role(this, 'ExecutionRole', {
        description: 'AI Persona advanced generation Code Interpreter (no permissions)',
        assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
          conditions: { StringEquals: { 'aws:SourceAccount': Stack.of(this).account } },
        }),
      });
    }

    this.resource = new agentcore.CfnCodeInterpreterCustom(this, 'Resource', {
      // AgentCore names start with a letter and allow up to 48 alphanumeric characters and _
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
