import { Construct } from 'constructs';
import { Stack, StackProps, Fn, CfnOutput } from 'aws-cdk-lib';
import * as agentcore from '@aws-cdk/aws-bedrock-agentcore-alpha';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import { AppParameter } from '../parameters';

export interface McpGatewayStackProps extends StackProps {
  parameter: AppParameter;
}

export class McpGatewayStack extends Stack {
  constructor(scope: Construct, id: string, props: McpGatewayStackProps) {
    super(scope, id, props);

    const { parameter } = props;

    // メインスタックからエクスポートされた値を取得
    const ecsEndpoint = Fn.importValue(`AIPersona-${parameter.envName}-EcsEndpoint`);
    const albArn = Fn.importValue(`AIPersona-${parameter.envName}-AlbArn`);

    // VPC Link V2（ALB への Private Integration）- L1で作成
    const vpcLink = new apigateway.CfnVpcLink(this, 'VpcLink', {
      name: `ai-persona-mcp-${parameter.envName}`,
      targetArns: [albArn],
    });

    // REST API（IAM 認証）
    const api = new apigateway.RestApi(this, 'Api', {
      restApiName: `ai-persona-mcp-${parameter.envName}`,
      description: 'AI Persona MCP API (AgentCore Gateway Target)',
      endpointTypes: [apigateway.EndpointType.REGIONAL],
      deployOptions: { stageName: 'v1' },
    });

    // /api/mcp/{proxy+} → ALB に転送
    const apiResource = api.root.addResource('api');
    const mcpResource = apiResource.addResource('mcp');
    const proxyResource = mcpResource.addResource('{proxy+}');

    const vpcLinkRef = apigateway.VpcLink.fromVpcLinkId(this, 'VpcLinkRef', vpcLink.ref);

    const integration = new apigateway.Integration({
      type: apigateway.IntegrationType.HTTP_PROXY,
      integrationHttpMethod: 'ANY',
      uri: Fn.join('', ['https://', ecsEndpoint, '/api/mcp/{proxy}']),
      options: {
        connectionType: apigateway.ConnectionType.VPC_LINK,
        vpcLink: vpcLinkRef,
        requestParameters: {
          'integration.request.path.proxy': 'method.request.path.proxy',
        },
      },
    });

    proxyResource.addMethod('ANY', integration, {
      authorizationType: apigateway.AuthorizationType.IAM,
      requestParameters: { 'method.request.path.proxy': true },
    });

    // /api/personas, /api/discussions（既存エンドポイント）も公開
    const personasResource = apiResource.addResource('personas');
    const personaIdResource = personasResource.addResource('{persona_id}');
    const discussionsResource = apiResource.addResource('discussions');

    const personasIntegration = new apigateway.Integration({
      type: apigateway.IntegrationType.HTTP_PROXY,
      integrationHttpMethod: 'ANY',
      uri: Fn.join('', ['https://', ecsEndpoint, '/api/personas']),
      options: { connectionType: apigateway.ConnectionType.VPC_LINK, vpcLink: vpcLinkRef },
    });
    personasResource.addMethod('GET', personasIntegration, {
      authorizationType: apigateway.AuthorizationType.IAM,
    });

    const personaIdIntegration = new apigateway.Integration({
      type: apigateway.IntegrationType.HTTP_PROXY,
      integrationHttpMethod: 'GET',
      uri: Fn.join('', ['https://', ecsEndpoint, '/api/personas/{persona_id}']),
      options: {
        connectionType: apigateway.ConnectionType.VPC_LINK,
        vpcLink: vpcLinkRef,
        requestParameters: {
          'integration.request.path.persona_id': 'method.request.path.persona_id',
        },
      },
    });
    personaIdResource.addMethod('GET', personaIdIntegration, {
      authorizationType: apigateway.AuthorizationType.IAM,
      requestParameters: { 'method.request.path.persona_id': true },
    });

    const discussionsIntegration = new apigateway.Integration({
      type: apigateway.IntegrationType.HTTP_PROXY,
      integrationHttpMethod: 'GET',
      uri: Fn.join('', ['https://', ecsEndpoint, '/api/discussions']),
      options: { connectionType: apigateway.ConnectionType.VPC_LINK, vpcLink: vpcLinkRef },
    });
    discussionsResource.addMethod('GET', discussionsIntegration, {
      authorizationType: apigateway.AuthorizationType.IAM,
    });

    // AgentCore Gateway（Cognito M2M 認証 + MCP プロトコル）
    const gateway = new agentcore.Gateway(this, 'Gateway', {
      gatewayName: `ai-persona-mcp-${parameter.envName}`,
      description: 'AI Persona MCP Gateway',
      protocolConfiguration: new agentcore.McpProtocolConfiguration({
        instructions:
          'AIペルソナシステムのMCPツール。ペルソナ生成、議論シミュレーション、インサイト生成が可能です。',
        searchType: agentcore.McpGatewaySearchType.SEMANTIC,
      }),
    });

    // API Gateway Target（IAM 認証 = GATEWAY_IAM_ROLE）
    gateway.addApiGatewayTarget('Target', {
      restApi: api,
      apiGatewayToolConfiguration: {
        toolFilters: [
          { filterPath: '/api/mcp/*', methods: [agentcore.ApiGatewayHttpMethod.GET, agentcore.ApiGatewayHttpMethod.POST] },
          { filterPath: '/api/personas', methods: [agentcore.ApiGatewayHttpMethod.GET] },
          { filterPath: '/api/personas/*', methods: [agentcore.ApiGatewayHttpMethod.GET] },
          { filterPath: '/api/discussions', methods: [agentcore.ApiGatewayHttpMethod.GET] },
        ],
      },
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
    new CfnOutput(this, 'ApiGatewayUrl', {
      value: api.url,
      description: 'API Gateway URL',
    });
    if (gateway.tokenEndpointUrl) {
      new CfnOutput(this, 'TokenEndpointUrl', {
        value: gateway.tokenEndpointUrl,
        description: 'Cognito Token Endpoint URL for M2M authentication',
      });
    }
  }
}
