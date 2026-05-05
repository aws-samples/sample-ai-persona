import { Construct } from 'constructs';
import { Stack, StackProps, Fn, CfnOutput } from 'aws-cdk-lib';
import * as agentcore from '@aws-cdk/aws-bedrock-agentcore-alpha';
import * as bedrockagentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as fs from 'fs';
import * as path from 'path';
import { AppParameter } from '../parameters';

export interface McpGatewayStackProps extends StackProps {
  parameter: AppParameter;
}

export class McpGatewayStack extends Stack {
  constructor(scope: Construct, id: string, props: McpGatewayStackProps) {
    super(scope, id, props);

    const { parameter } = props;

    // メインスタックからECSエンドポイントを取得
    const ecsEndpoint = Fn.importValue(
      `AIPersona-${parameter.envName}-EcsEndpoint`,
    );

    // Gateway（デフォルト: Cognito M2M認証 + MCPプロトコル）
    const gateway = new agentcore.Gateway(this, 'Gateway', {
      gatewayName: `ai-persona-mcp-${parameter.envName}`,
      description: 'AI Persona MCP Gateway',
      protocolConfiguration: new agentcore.McpProtocolConfiguration({
        instructions:
          'AIペルソナシステムのMCPツール。ペルソナ生成、議論シミュレーション、インサイト生成が可能です。',
        searchType: agentcore.McpGatewaySearchType.SEMANTIC,
      }),
    });

    // OpenAPI spec を読み込み、servers を ECS エンドポイントに置換
    const specPath = path.join(__dirname, '..', '..', 'openapi_mcp.json');
    const specRaw = fs.readFileSync(specPath, 'utf-8');
    const spec = JSON.parse(specRaw);
    // servers フィールドを ${EcsEndpoint} プレースホルダーに置換
    // NOTE: Fn.sub は ${...} パターンを全て置換するため、
    // OpenAPI spec内のdescription等に ${...} を含めないこと
    spec.servers = [{ url: '${EcsEndpoint}' }];
    const specTemplate = JSON.stringify(spec);

    // Fn.sub で ECS エンドポイントを埋め込み
    const inlinePayload = Fn.sub(specTemplate, {
      EcsEndpoint: ecsEndpoint,
    });

    // L1 CfnGatewayTarget で OpenAPI Target を作成
    // （L2 addOpenApiTarget は CloudFormation token を servers に埋め込めないため）
    new bedrockagentcore.CfnGatewayTarget(this, 'Target', {
      gatewayIdentifier: gateway.gatewayId,
      name: `ai-persona-api-${parameter.envName}`,
      description: 'AI Persona REST API on ECS Express Mode',
      targetConfiguration: {
        mcp: {
          openApiSchema: {
            inlinePayload,
          },
        },
      },
      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],
    });

    // --- Outputs ---
    new CfnOutput(this, 'GatewayId', {
      value: gateway.gatewayId,
      description: 'AgentCore Gateway ID',
    });
    new CfnOutput(this, 'GatewayArn', {
      value: gateway.gatewayArn,
      description: 'AgentCore Gateway ARN',
    });
    if (gateway.tokenEndpointUrl) {
      new CfnOutput(this, 'TokenEndpointUrl', {
        value: gateway.tokenEndpointUrl,
        description: 'Cognito Token Endpoint URL for M2M authentication',
      });
    }
  }
}
