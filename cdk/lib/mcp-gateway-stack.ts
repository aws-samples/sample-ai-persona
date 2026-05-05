import { Construct } from 'constructs';
import { Stack, StackProps, Fn, CfnOutput } from 'aws-cdk-lib';
import * as agentcore from '@aws-cdk/aws-bedrock-agentcore-alpha';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as apigatewayv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
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

    // VPC をルックアップ（VPC Link V2 にはサブネットとSGが必要）
    const vpc = ec2.Vpc.fromLookup(this, 'Vpc', {
      vpcName: `ai-persona-${parameter.envName}`,
    });

    // VPC Link V2 用セキュリティグループ
    const vpcLinkSg = new ec2.SecurityGroup(this, 'VpcLinkSg', {
      vpc,
      description: 'Security group for API Gateway VPC Link to ALB',
      allowAllOutbound: true,
    });
    // ALB のリスナーポート（443）への通信を許可
    vpcLinkSg.addEgressRule(ec2.Peer.ipv4(vpc.vpcCidrBlock), ec2.Port.tcp(443));

    // VPC Link V2（ALB サポート）
    const vpcLinkV2 = new apigatewayv2.CfnVpcLink(this, 'VpcLink', {
      name: `ai-persona-mcp-${parameter.envName}`,
      subnetIds: vpc.privateSubnets.map(s => s.subnetId),
      securityGroupIds: [vpcLinkSg.securityGroupId],
    });

    // REST API（IAM 認証）
    const api = new apigateway.RestApi(this, 'Api', {
      restApiName: `ai-persona-mcp-${parameter.envName}`,
      description: 'AI Persona MCP API (AgentCore Gateway Target)',
      endpointTypes: [apigateway.EndpointType.REGIONAL],
      deployOptions: { stageName: 'v1' },
    });

    // ヘルパー: VPC Link V2 + ALB の Private Integration を作成
    const vpcLinkId = vpcLinkV2.ref;

    const createAlbIntegration = (
      method: string,
      path: string,
      requestParams?: Record<string, string>,
    ) => new apigateway.Integration({
      type: apigateway.IntegrationType.HTTP_PROXY,
      integrationHttpMethod: method,
      uri: Fn.join('', ['https://', ecsEndpoint, path]),
      options: {
        connectionType: apigateway.ConnectionType.VPC_LINK,
        vpcLink: apigateway.VpcLink.fromVpcLinkId(this, `VL-${path}-${method}`, vpcLinkId),
        requestParameters: requestParams,
      },
    });

    // /api/mcp/{proxy+} → ALB に転送
    const apiResource = api.root.addResource('api');
    const mcpResource = apiResource.addResource('mcp');
    const proxyResource = mcpResource.addResource('{proxy+}');

    const proxyMethod = proxyResource.addMethod('ANY', createAlbIntegration('ANY', '/api/mcp/{proxy}', {
      'integration.request.path.proxy': 'method.request.path.proxy',
    }), {
      authorizationType: apigateway.AuthorizationType.IAM,
      requestParameters: { 'method.request.path.proxy': true },
    });

    // /api/personas
    const personasResource = apiResource.addResource('personas');
    const personasMethod = personasResource.addMethod('GET', createAlbIntegration('GET', '/api/personas'), {
      authorizationType: apigateway.AuthorizationType.IAM,
    });

    // /api/personas/{persona_id}
    const personaIdResource = personasResource.addResource('{persona_id}');
    const personaIdMethod = personaIdResource.addMethod('GET', createAlbIntegration('GET', '/api/personas/{persona_id}', {
      'integration.request.path.persona_id': 'method.request.path.persona_id',
    }), {
      authorizationType: apigateway.AuthorizationType.IAM,
      requestParameters: { 'method.request.path.persona_id': true },
    });

    // /api/discussions
    const discussionsResource = apiResource.addResource('discussions');
    const discussionsMethod = discussionsResource.addMethod('GET', createAlbIntegration('GET', '/api/discussions'), {
      authorizationType: apigateway.AuthorizationType.IAM,
    });

    // L1 escape hatch: IntegrationTarget（ALB ARN）を設定
    for (const method of [proxyMethod, personasMethod, personaIdMethod, discussionsMethod]) {
      const cfnMethod = method.node.defaultChild as apigateway.CfnMethod;
      cfnMethod.addPropertyOverride('Integration.IntegrationTarget', albArn);
    }

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
