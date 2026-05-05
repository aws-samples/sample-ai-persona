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

    // リクエストボディモデル（Gateway がボディを転送するために必要）
    const generatePersonasModel = api.addModel('GeneratePersonasModel', {
      contentType: 'application/json',
      modelName: 'GeneratePersonasRequest',
      schema: {
        type: apigateway.JsonSchemaType.OBJECT,
        properties: {
          data_type: { type: apigateway.JsonSchemaType.STRING, description: 'データ種別: interview, market_report, review, purchase, other' },
          file_contents: { type: apigateway.JsonSchemaType.ARRAY, items: { type: apigateway.JsonSchemaType.STRING }, description: 'テキストデータのリスト' },
          count: { type: apigateway.JsonSchemaType.INTEGER, description: '生成するペルソナ数 (1-10)' },
          description: { type: apigateway.JsonSchemaType.STRING, description: 'データの説明' },
          custom_prompt: { type: apigateway.JsonSchemaType.STRING, description: 'カスタムプロンプト' },
        },
        required: ['data_type'],
      },
    });

    const runDiscussionModel = api.addModel('RunDiscussionModel', {
      contentType: 'application/json',
      modelName: 'RunDiscussionRequest',
      schema: {
        type: apigateway.JsonSchemaType.OBJECT,
        properties: {
          persona_ids: { type: apigateway.JsonSchemaType.ARRAY, items: { type: apigateway.JsonSchemaType.STRING }, description: '参加ペルソナIDリスト（2名以上）' },
          topic: { type: apigateway.JsonSchemaType.STRING, description: '議論トピック' },
          mode: { type: apigateway.JsonSchemaType.STRING, description: '議論モード: classic または agent' },
        },
        required: ['persona_ids', 'topic'],
      },
    });

    const generateInsightsModel = api.addModel('GenerateInsightsModel', {
      contentType: 'application/json',
      modelName: 'GenerateInsightsRequest',
      schema: {
        type: apigateway.JsonSchemaType.OBJECT,
        properties: {
          categories: { type: apigateway.JsonSchemaType.ARRAY, items: { type: apigateway.JsonSchemaType.OBJECT }, description: 'カスタムインサイトカテゴリ' },
        },
      },
    });

    const runInterviewModel = api.addModel('RunInterviewModel', {
      contentType: 'application/json',
      modelName: 'RunInterviewRequest',
      schema: {
        type: apigateway.JsonSchemaType.OBJECT,
        properties: {
          persona_ids: { type: apigateway.JsonSchemaType.ARRAY, items: { type: apigateway.JsonSchemaType.STRING }, description: '参加ペルソナIDリスト（1-5名）' },
          question: { type: apigateway.JsonSchemaType.STRING, description: '質問内容' },
        },
        required: ['persona_ids', 'question'],
      },
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

    // /api/mcp/personas/generate (POST)
    const apiResource = api.root.addResource('api');
    const mcpResource = apiResource.addResource('mcp');
    const mcpPersonasResource = mcpResource.addResource('personas');
    const generateResource = mcpPersonasResource.addResource('generate');
    const generateMethod = generateResource.addMethod('POST', createAlbIntegration('POST', '/api/mcp/personas/generate'), {
      authorizationType: apigateway.AuthorizationType.IAM,
      methodResponses: [{ statusCode: '200' }, { statusCode: '202' }],
      operationName: 'generatePersonas',
      requestModels: { 'application/json': generatePersonasModel },
    });

    // /api/mcp/discussions (POST)
    const mcpDiscussionsResource = mcpResource.addResource('discussions');
    const runDiscussionMethod = mcpDiscussionsResource.addMethod('POST', createAlbIntegration('POST', '/api/mcp/discussions'), {
      authorizationType: apigateway.AuthorizationType.IAM,
      methodResponses: [{ statusCode: '202' }],
      operationName: 'runDiscussion',
      requestModels: { 'application/json': runDiscussionModel },
    });

    // /api/mcp/discussions/{discussion_id} (GET)
    const mcpDiscussionIdResource = mcpDiscussionsResource.addResource('{discussion_id}');
    const getDiscussionMethod = mcpDiscussionIdResource.addMethod('GET', createAlbIntegration('GET', '/api/mcp/discussions/{discussion_id}', {
      'integration.request.path.discussion_id': 'method.request.path.discussion_id',
    }), {
      authorizationType: apigateway.AuthorizationType.IAM,
      requestParameters: { 'method.request.path.discussion_id': true },
      methodResponses: [{ statusCode: '200' }],
      operationName: 'getDiscussion',
    });

    // /api/mcp/discussions/{discussion_id}/insights (POST)
    const insightsResource = mcpDiscussionIdResource.addResource('insights');
    const generateInsightsMethod = insightsResource.addMethod('POST', createAlbIntegration('POST', '/api/mcp/discussions/{discussion_id}/insights', {
      'integration.request.path.discussion_id': 'method.request.path.discussion_id',
    }), {
      authorizationType: apigateway.AuthorizationType.IAM,
      requestParameters: { 'method.request.path.discussion_id': true },
      methodResponses: [{ statusCode: '200' }],
      operationName: 'generateInsights',
      requestModels: { 'application/json': generateInsightsModel },
    });

    // /api/mcp/interviews (POST)
    const interviewsResource = mcpResource.addResource('interviews');
    const runInterviewMethod = interviewsResource.addMethod('POST', createAlbIntegration('POST', '/api/mcp/interviews'), {
      authorizationType: apigateway.AuthorizationType.IAM,
      methodResponses: [{ statusCode: '200' }],
      operationName: 'runInterview',
      requestModels: { 'application/json': runInterviewModel },
    });

    // /api/mcp/jobs/{job_id} (GET)
    const jobsResource = mcpResource.addResource('jobs');
    const jobIdResource = jobsResource.addResource('{job_id}');
    const getJobMethod = jobIdResource.addMethod('GET', createAlbIntegration('GET', '/api/mcp/jobs/{job_id}', {
      'integration.request.path.job_id': 'method.request.path.job_id',
    }), {
      authorizationType: apigateway.AuthorizationType.IAM,
      requestParameters: { 'method.request.path.job_id': true },
      methodResponses: [{ statusCode: '200' }],
      operationName: 'getJobStatus',
    });

    // /api/personas
    const personasResource = apiResource.addResource('personas');
    const personasMethod = personasResource.addMethod('GET', createAlbIntegration('GET', '/api/personas'), {
      authorizationType: apigateway.AuthorizationType.IAM,
      methodResponses: [{ statusCode: '200' }],
      operationName: 'listPersonas',
    });

    // /api/personas/{persona_id}
    const personaIdResource = personasResource.addResource('{persona_id}');
    const personaIdMethod = personaIdResource.addMethod('GET', createAlbIntegration('GET', '/api/personas/{persona_id}', {
      'integration.request.path.persona_id': 'method.request.path.persona_id',
    }), {
      authorizationType: apigateway.AuthorizationType.IAM,
      requestParameters: { 'method.request.path.persona_id': true },
      methodResponses: [{ statusCode: '200' }],
      operationName: 'getPersona',
    });

    // /api/discussions
    const discussionsResource = apiResource.addResource('discussions');
    const discussionsMethod = discussionsResource.addMethod('GET', createAlbIntegration('GET', '/api/discussions'), {
      authorizationType: apigateway.AuthorizationType.IAM,
      methodResponses: [{ statusCode: '200' }],
      operationName: 'listDiscussions',
    });

    // L1 escape hatch: IntegrationTarget（ALB ARN）を設定
    for (const method of [generateMethod, runDiscussionMethod, getDiscussionMethod, generateInsightsMethod, runInterviewMethod, getJobMethod, personasMethod, personaIdMethod, discussionsMethod]) {
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
      credentialProviderConfigurations: [
        agentcore.GatewayCredentialProvider.fromIamRole(),
      ],
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
