import { Construct } from 'constructs';
import { Stack, StackProps, Fn, CfnOutput } from 'aws-cdk-lib';
import * as agentcore from '@aws-cdk/aws-bedrock-agentcore-alpha';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as apigatewayv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { AppParameter } from '../parameters';

// ---------------------------------------------------------------------------
// Endpoint definitions (single source of truth)
// ---------------------------------------------------------------------------

interface EndpointDef {
  /** Path segments under /api/ (e.g. 'personas/generate') */
  path: string;
  method: 'GET' | 'POST';
  operationName: string;
  summary: string;
  description: string;
  /** Path parameters (e.g. ['discussion_id']) */
  pathParams?: string[];
  /** Request body JSON schema (POST only) */
  requestSchema?: apigateway.JsonSchema;
}

const ENDPOINTS: EndpointDef[] = [
  {
    path: 'personas',
    method: 'GET',
    operationName: 'listPersonas',
    summary: 'List all saved personas',
    description: 'Retrieve a list of all saved AI personas with their demographics, values, pain points, and goals. Use persona IDs from this list for discussions and interviews.',
  },
  {
    path: 'personas/{persona_id}',
    method: 'GET',
    operationName: 'getPersona',
    summary: 'Get persona details by ID',
    description: 'Retrieve detailed information about a specific persona including background, values, pain points, and goals.',
    pathParams: ['persona_id'],
  },
  {
    path: 'personas/generate',
    method: 'POST',
    operationName: 'generatePersonas',
    summary: 'Generate AI personas from text data (async)',
    description: 'Generate AI personas from interview transcripts, market reports, or other text data. Returns a job_id immediately. Poll getJobStatus with the job_id to check progress and retrieve results.',
    requestSchema: {
      type: apigateway.JsonSchemaType.OBJECT,
      properties: {
        data_type: { type: apigateway.JsonSchemaType.STRING, description: 'Data type: interview, market_report, review, purchase, other' },
        file_contents: { type: apigateway.JsonSchemaType.ARRAY, items: { type: apigateway.JsonSchemaType.STRING }, description: 'List of text data (each element represents file content)' },
        count: { type: apigateway.JsonSchemaType.INTEGER, description: 'Number of personas to generate (1-10)' },
        description: { type: apigateway.JsonSchemaType.STRING, description: 'Data description (used when data_type=other)' },
        custom_prompt: { type: apigateway.JsonSchemaType.STRING, description: 'Custom prompt for generation' },
      },
      required: ['data_type'],
    },
  },
  {
    path: 'discussions',
    method: 'GET',
    operationName: 'listDiscussions',
    summary: 'List all saved discussions',
    description: 'Retrieve a list of all saved discussions with their topics, modes, and creation dates. Use discussion IDs from this list to get full details or generate insights.',
  },
  {
    path: 'discussions',
    method: 'POST',
    operationName: 'runDiscussion',
    summary: 'Run a discussion between personas (async)',
    description: 'Start a simulated discussion between specified personas on a given topic. Supports "classic" (fast, 1-3 min) and "agent" (deep, 5-15 min) modes. Returns a job_id. Poll getJobStatus to get the full discussion with messages and insights.',
    requestSchema: {
      type: apigateway.JsonSchemaType.OBJECT,
      properties: {
        persona_ids: { type: apigateway.JsonSchemaType.ARRAY, items: { type: apigateway.JsonSchemaType.STRING }, description: 'Persona ID list (2 or more)' },
        topic: { type: apigateway.JsonSchemaType.STRING, description: 'Discussion topic' },
        mode: { type: apigateway.JsonSchemaType.STRING, description: 'Discussion mode: classic or agent' },
      },
      required: ['persona_ids', 'topic'],
    },
  },
  {
    path: 'discussions/{discussion_id}',
    method: 'GET',
    operationName: 'getDiscussion',
    summary: 'Get discussion details with messages and insights',
    description: 'Retrieve a saved discussion including all messages exchanged between personas and generated insights with confidence scores.',
    pathParams: ['discussion_id'],
  },
  {
    path: 'discussions/{discussion_id}/insights',
    method: 'POST',
    operationName: 'generateInsights',
    summary: 'Generate insights from a discussion',
    description: 'Analyze a saved discussion and generate structured insights categorized by customer needs, market opportunities, product development, and marketing. Optionally provide custom categories.',
    pathParams: ['discussion_id'],
    requestSchema: {
      type: apigateway.JsonSchemaType.OBJECT,
      properties: {
        categories: { type: apigateway.JsonSchemaType.ARRAY, items: { type: apigateway.JsonSchemaType.OBJECT }, description: 'Custom insight categories [{name, description}, ...]' },
      },
    },
  },
  {
    path: 'interviews',
    method: 'POST',
    operationName: 'runInterview',
    summary: 'Interview personas with a question',
    description: 'Send a question to one or more personas (1-5) and receive their responses. Each persona answers based on their background, values, and pain points. Useful for quick Q&A without running a full discussion.',
    requestSchema: {
      type: apigateway.JsonSchemaType.OBJECT,
      properties: {
        persona_ids: { type: apigateway.JsonSchemaType.ARRAY, items: { type: apigateway.JsonSchemaType.STRING }, description: 'Persona ID list (1-5)' },
        question: { type: apigateway.JsonSchemaType.STRING, description: 'Question to ask' },
      },
      required: ['persona_ids', 'question'],
    },
  },
  {
    path: 'jobs/{job_id}',
    method: 'GET',
    operationName: 'getJobStatus',
    summary: 'Check async job status and results',
    description: 'Check the status of an async job (generatePersonas or runDiscussion). Status values: "pending", "running", "completed", "failed". When completed, the result field contains the full output.',
    pathParams: ['job_id'],
  },
];

// ---------------------------------------------------------------------------
// Stack
// ---------------------------------------------------------------------------

export interface McpGatewayStackProps extends StackProps {
  parameter: AppParameter;
}

export class McpGatewayStack extends Stack {
  constructor(scope: Construct, id: string, props: McpGatewayStackProps) {
    super(scope, id, props);

    const { parameter } = props;
    const ecsEndpoint = Fn.importValue(`AIPersona-${parameter.envName}-EcsEndpoint`);
    const albArn = Fn.importValue(`AIPersona-${parameter.envName}-AlbArn`);

    // --- VPC Link V2 (ALB support) ---
    const vpc = ec2.Vpc.fromLookup(this, 'Vpc', {
      vpcName: `ai-persona-${parameter.envName}`,
    });
    const vpcLinkSg = new ec2.SecurityGroup(this, 'VpcLinkSg', {
      vpc,
      description: 'Security group for API Gateway VPC Link to ALB',
    });
    const vpcLinkV2 = new apigatewayv2.CfnVpcLink(this, 'VpcLink', {
      name: `ai-persona-mcp-${parameter.envName}`,
      subnetIds: vpc.privateSubnets.map(s => s.subnetId),
      securityGroupIds: [vpcLinkSg.securityGroupId],
    });
    const vpcLinkRef = apigateway.VpcLink.fromVpcLinkId(this, 'VpcLinkRef', vpcLinkV2.ref);

    // --- REST API ---
    const api = new apigateway.RestApi(this, 'Api', {
      restApiName: `ai-persona-mcp-${parameter.envName}`,
      description: 'AI Persona MCP API (AgentCore Gateway Target)',
      endpointTypes: [apigateway.EndpointType.REGIONAL],
      deployOptions: { stageName: 'v1', documentationVersion: 'v1' },
    });

    // --- Build resources & methods from endpoint definitions ---
    const apiRoot = api.root.addResource('api');
    const resourceCache: Record<string, apigateway.IResource> = {};

    /** Recursively get or create nested API Gateway resources */
    const getResource = (pathStr: string): apigateway.IResource => {
      if (resourceCache[pathStr]) return resourceCache[pathStr];
      const segments = pathStr.split('/');
      if (segments.length === 1) {
        resourceCache[pathStr] = apiRoot.addResource(segments[0]);
      } else {
        const parent = getResource(segments.slice(0, -1).join('/'));
        resourceCache[pathStr] = parent.addResource(segments[segments.length - 1]);
      }
      return resourceCache[pathStr];
    };

    // Request body models (created lazily per endpoint)
    const models: Record<string, apigateway.IModel> = {};

    const methods: apigateway.Method[] = [];
    const docParts: apigateway.CfnDocumentationPart[] = [];

    for (const ep of ENDPOINTS) {
      const resource = getResource(ep.path);
      const backendPath = `/api/${ep.path}`;

      // Path parameter mappings
      const requestParams: Record<string, boolean> = {};
      const integrationParams: Record<string, string> = {};
      for (const param of ep.pathParams ?? []) {
        requestParams[`method.request.path.${param}`] = true;
        integrationParams[`integration.request.path.${param}`] = `method.request.path.${param}`;
      }

      // Integration
      const integration = new apigateway.Integration({
        type: apigateway.IntegrationType.HTTP_PROXY,
        integrationHttpMethod: ep.method,
        uri: Fn.join('', ['https://', ecsEndpoint, backendPath]),
        options: {
          connectionType: apigateway.ConnectionType.VPC_LINK,
          vpcLink: vpcLinkRef,
          requestParameters: Object.keys(integrationParams).length > 0 ? integrationParams : undefined,
        },
      });

      // Request model (POST with schema)
      let requestModels: Record<string, apigateway.IModel> | undefined;
      if (ep.requestSchema) {
        const modelId = `${ep.operationName}Model`;
        models[modelId] = api.addModel(modelId, {
          contentType: 'application/json',
          modelName: `${ep.operationName}Request`,
          schema: ep.requestSchema,
        });
        requestModels = { 'application/json': models[modelId] };
      }

      // Method
      const method = resource.addMethod(ep.method, integration, {
        authorizationType: apigateway.AuthorizationType.IAM,
        operationName: ep.operationName,
        methodResponses: [{ statusCode: '200' }],
        requestParameters: Object.keys(requestParams).length > 0 ? requestParams : undefined,
        requestModels,
      });
      methods.push(method);

      // L1: IntegrationTarget (ALB ARN)
      const cfnMethod = method.node.defaultChild as apigateway.CfnMethod;
      cfnMethod.addPropertyOverride('Integration.IntegrationTarget', albArn);

      // Documentation part
      const part = new apigateway.CfnDocumentationPart(this, `Doc-${ep.operationName}`, {
        restApiId: api.restApiId,
        location: { type: 'METHOD', path: `/api/${ep.path}`, method: ep.method },
        properties: JSON.stringify({ summary: ep.summary, description: ep.description }),
      });
      docParts.push(part);
    }

    // Documentation version (must be created AFTER all parts)
    const docVersion = new apigateway.CfnDocumentationVersion(this, 'DocVersion', {
      restApiId: api.restApiId,
      documentationVersion: 'v1',
    });
    for (const part of docParts) {
      docVersion.addDependency(part);
    }

    // --- AgentCore Gateway ---
    const gateway = new agentcore.Gateway(this, 'Gateway', {
      gatewayName: `ai-persona-mcp-${parameter.envName}`,
      description: 'AI Persona research toolkit for generating personas, running discussions, and extracting insights',
      protocolConfiguration: new agentcore.McpProtocolConfiguration({
        instructions: [
          'This gateway provides AI persona research tools for product planning and marketing strategy.',
          'Use these tools when you need to: (1) generate realistic customer personas from interview data or reports,',
          '(2) simulate multi-persona discussions on a topic to surface diverse viewpoints,',
          '(3) extract structured insights (customer needs, market opportunities, product ideas) from discussions,',
          '(4) conduct quick Q&A interviews with existing personas.',
          '',
          'Typical workflow: listPersonas -> runDiscussion (async, poll getJobStatus) -> getDiscussion.',
          'For new personas: generatePersonas (async, poll getJobStatus) -> listPersonas.',
          'For quick answers: listPersonas -> runInterview.',
        ].join(' '),
        searchType: agentcore.McpGatewaySearchType.SEMANTIC,
      }),
    });

    gateway.addApiGatewayTarget('Target', {
      restApi: api,
      credentialProviderConfigurations: [
        agentcore.GatewayCredentialProvider.fromIamRole(),
      ],
      apiGatewayToolConfiguration: {
        toolFilters: [
          { filterPath: '/api/*', methods: [agentcore.ApiGatewayHttpMethod.GET, agentcore.ApiGatewayHttpMethod.POST] },
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
