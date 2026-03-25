import { Construct } from 'constructs';
import { Stack, StackProps, CfnOutput, RemovalPolicy } from 'aws-cdk-lib';
import { AgentCoreMemory } from './constructs/agentcore-memory';
import { AppParameter } from '../parameters';

export interface AgentCoreMemoryStackProps extends StackProps {
  parameter: AppParameter;
}

/**
 * AgentCore Memory Stack
 * 
 * このスタックは独立してデプロイされ、Memory IDとStrategy IDを出力します。
 * デプロイ後、出力されたIDをparameters.tsに手動で設定してから、
 * メインスタックをデプロイしてください。
 */
export class AgentCoreMemoryStack extends Stack {
  public readonly memoryId: string;
  public readonly summaryStrategyId: string;
  public readonly semanticStrategyId: string;

  constructor(scope: Construct, id: string, props: AgentCoreMemoryStackProps) {
    super(scope, id, props);

    const { parameter } = props;
    const isProd = parameter.envName === 'prod';

    // AgentCore Memory作成
    const agentCoreMemory = new AgentCoreMemory(this, 'Memory', {
      namePrefix: 'AIPersona',
      envName: parameter.envName,
      eventExpiryDuration: parameter.agentCoreMemoryEventExpiryDays ?? 90,
      description: `Long-term memory for AI Persona ${parameter.envName} environment`,
      enableSummaryStrategy: true,
      enableSemanticStrategy: true,
      removalPolicy: isProd ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    });

    this.memoryId = agentCoreMemory.memoryId;
    this.summaryStrategyId = agentCoreMemory.summaryStrategyId || '';
    this.semanticStrategyId = agentCoreMemory.semanticStrategyId || '';

    // Memory IDを出力
    new CfnOutput(this, 'MemoryId', {
      value: agentCoreMemory.memoryId,
      description: 'AgentCore Memory ID - Copy this to parameters.ts as agentCoreMemoryId',
      exportName: `${id}-MemoryId`,
    });

    // 重要な注意事項を出力
    new CfnOutput(this, 'NextSteps', {
      value: 'IMPORTANT: After deployment, run "aws bedrock-agentcore-control get-memory --memory-id <MEMORY_ID>" to get the Strategy ID, then update parameters.ts',
      description: 'Next steps to complete setup',
    });

    // AWS CLIコマンドのヘルプを出力
    new CfnOutput(this, 'GetStrategyIdCommand', {
      value: `aws bedrock-agentcore-control get-memory --memory-id ${agentCoreMemory.memoryId} --region ${this.region} --query 'memory.strategies[?type==\`SUMMARIZATION\`].strategyId' --output text`,
      description: 'Run this command to get the Summary Strategy ID',
    });

    // Semantic Strategy ID取得コマンドを出力
    new CfnOutput(this, 'GetSemanticStrategyIdCommand', {
      value: `aws bedrock-agentcore-control get-memory --memory-id ${agentCoreMemory.memoryId} --region ${this.region} --query 'memory.strategies[?type==\`SEMANTIC\`].strategyId' --output text`,
      description: 'Run this command to get the Semantic Strategy ID',
    });
  }
}
