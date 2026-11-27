import * as iam from 'aws-cdk-lib/aws-iam';

/**
 * Policies for the closed-network VPC endpoints.
 *
 * The 12 endpoints the app uses get a policy that limits access to this app's resources. The
 * principal is not restricted. Resource statements allow the service's `*` action; list-API
 * statements use Resource `*` with only that API's action. ARNs are never written by hand:
 * AppStack builds an EndpointAccessScope from the same references as the task role permissions.
 *
 * Only pure functions live here, with no CDK constructs (test/endpoint-policies.test.ts).
 * ClosedNetwork.restrictEndpoints attaches the policies.
 */

/**
 * List APIs that cannot take a resource (the task role's `resources: ['*']` statement).
 * Both AppStack.grantTaskRole and the endpoint policies use this constant. The order matches
 * the task role statement so the public template does not change.
 */
export const TASK_LIST_ACTIONS: readonly string[] = Object.freeze([
  'dynamodb:ListTables',
  // The three bedrock list APIs (ListFoundationModels etc.) are left out: the app code never calls them
  'bedrock-agentcore:ListMemories',
]);

/** List API of the execution role (CDK adds it for pulling ECR images; Resource is `*`). */
export const EXECUTION_LIST_ACTIONS: readonly string[] = Object.freeze(['ecr:GetAuthorizationToken']);

/**
 * APIs the app calls that IAM does not authorize. No role statement covers them, so they are
 * kept here. Currently only cognito-idp:RevokeToken (revoke in
 * src/services/cognito/cognito_auth_service.py; the request is authorized by the client ID and secret).
 */
export const UNSIGNED_ACTIONS: readonly string[] = Object.freeze(['cognito-idp:RevokeToken']);

/** Endpoints restricted by policy: 2 gateway types and the 10 interface types the app uses. */
export type RestrictedEndpointService =
  | 's3'
  | 'dynamodb'
  | 'ecr.api'
  | 'ecr.dkr'
  | 'logs'
  | 'secretsmanager'
  | 'cognito-idp'
  | 'bedrock-runtime'
  | 'states'
  | 'bedrock-agent-runtime'
  | 'bedrock-agentcore'
  | 'bedrock-agentcore-control';

/**
 * Access targets the endpoint policies allow.
 *
 * List APIs and unsigned APIs come from the same constants as the roles (TASK_LIST_ACTIONS,
 * EXECUTION_LIST_ACTIONS, UNSIGNED_ACTIONS), so they are not held here.
 */
export interface EndpointAccessScope {
  /** The app's upload bucket (objects are <ARN>/*). */
  readonly bucketArn: string;
  /** AWS-managed bucket where ECR stores image layers (ecrLayerBucketArn); read-only. */
  readonly ecrLayerBucketArn: string;
  /** All tables the task role reads and writes (GSIs add <ARN>/index/*). */
  readonly tableArns: readonly string[];
  readonly userPoolArn: string;
  /** All secrets the execution role reads at task start. */
  readonly secretArns: readonly string[];
  /** The app's log group ARNs (both the log group ARN and <ARN>:* covering its log streams). */
  readonly logGroupArns: readonly string[];
  readonly repositoryArn: string;
  /** This Stage's AgentCore Memory (children add <ARN>/*). */
  readonly memoryArn: string;
  /** The BedrockArns list itself. */
  readonly inferenceArns: readonly string[];
  /** knowledge-base/* in this account and region (same as the task role). */
  readonly knowledgeBaseArn: string;
  /** The survey state machine (StartExecution; same as the task role). */
  readonly stateMachineArn: string;
  /** Executions of that state machine (DescribeExecution, StopExecution; same as the task role). */
  readonly stateMachineExecutionsArn: string;
}

/** The AWS-managed bucket where ECR stores image layers (`prod-<region>-starport-layer-bucket`). */
export function ecrLayerBucketArn(partition: string, region: string): string {
  return `arn:${partition}:s3:::prod-${region}-starport-layer-bucket`;
}

/** One endpoint's statements: `<prefix>:*` on its resources, and its list APIs on `*`. */
interface EndpointPolicyShape {
  /** The IAM action prefix of the resource statement (`<prefix>:*`). */
  readonly prefix: string;
  readonly resources: (scope: EndpointAccessScope) => readonly string[];
  /** Actions allowed on Resource `*` (list APIs and unsigned APIs). */
  readonly listActions?: readonly string[];
}

const listActionsOf = (prefix: string, actions: readonly string[]) =>
  actions.filter((action) => action.startsWith(`${prefix}:`));

const agentCore: EndpointPolicyShape = {
  prefix: 'bedrock-agentcore',
  resources: (s) => [s.memoryArn, `${s.memoryArn}/*`],
  listActions: listActionsOf('bedrock-agentcore', TASK_LIST_ACTIONS),
};
const ecr: EndpointPolicyShape = {
  prefix: 'ecr',
  resources: (s) => [s.repositoryArn],
  listActions: EXECUTION_LIST_ACTIONS,
};

/** Per-endpoint policy shapes (the S3 layer bucket statement is added in endpointPolicyStatements). */
const POLICY_SHAPES: Readonly<Record<RestrictedEndpointService, EndpointPolicyShape>> = Object.freeze({
  s3: { prefix: 's3', resources: (s) => [s.bucketArn, `${s.bucketArn}/*`] },
  dynamodb: {
    prefix: 'dynamodb',
    resources: (s) => [...s.tableArns, ...s.tableArns.map((table) => `${table}/index/*`)],
    listActions: listActionsOf('dynamodb', TASK_LIST_ACTIONS),
  },
  'ecr.api': ecr,
  'ecr.dkr': ecr,
  logs: { prefix: 'logs', resources: (s) => s.logGroupArns },
  secretsmanager: { prefix: 'secretsmanager', resources: (s) => s.secretArns },
  'cognito-idp': { prefix: 'cognito-idp', resources: (s) => [s.userPoolArn], listActions: UNSIGNED_ACTIONS },
  'bedrock-runtime': { prefix: 'bedrock', resources: (s) => s.inferenceArns },
  // Starting, checking and stopping survey executions; narrowing the actions is left to the task role
  states: {
    prefix: 'states',
    resources: (s) => [s.stateMachineArn, s.stateMachineExecutionsArn],
  },
  'bedrock-agent-runtime': { prefix: 'bedrock', resources: (s) => [s.knowledgeBaseArn] },
  'bedrock-agentcore': agentCore,
  'bedrock-agentcore-control': agentCore,
});

/** The endpoints that get a policy; the three SSM endpoints are not in it. */
export const RESTRICTED_ENDPOINT_SERVICES: readonly RestrictedEndpointService[] = Object.freeze(
  Object.keys(POLICY_SHAPES) as RestrictedEndpointService[],
);

/**
 * The statements of one endpoint's policy: `<prefix>:*` on the app's
 * resources, the list or unsigned APIs on `*`, and for S3 `s3:GetObject` on the ECR layer bucket.
 * Every statement allows any principal.
 */
export function endpointPolicyStatements(
  service: RestrictedEndpointService,
  scope: EndpointAccessScope,
): iam.PolicyStatement[] {
  const shape = POLICY_SHAPES[service];
  const statements = [allow([`${shape.prefix}:*`], shape.resources(scope))];
  if (shape.listActions && shape.listActions.length > 0) {
    statements.push(allow(shape.listActions, ['*']));
  }
  if (service === 's3') {
    // The task pulls image layers from here (read-only)
    statements.push(allow(['s3:GetObject'], [`${scope.ecrLayerBucketArn}/*`]));
  }
  return statements;
}

function allow(actions: readonly string[], resources: readonly string[]): iam.PolicyStatement {
  return new iam.PolicyStatement({
    effect: iam.Effect.ALLOW,
    principals: [new iam.StarPrincipal()],
    actions: [...actions],
    resources: [...resources],
  });
}
