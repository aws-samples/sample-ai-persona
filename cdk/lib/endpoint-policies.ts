import * as iam from 'aws-cdk-lib/aws-iam';

/**
 * 閉域の VPC エンドポイントのポリシー（閉域対応 U2、NFR2、FR2.3、functional-spec.md §3、BR1.1〜BR1.9）。
 *
 * アプリが使う 12 種のエンドポイントに、アクセス先をこのアプリの資源に絞るポリシーを付ける。
 * 使う人（プリンシパル）は絞らない（BR1.2）。資源の文は Action がそのサービスの `*`、一覧の API の
 * 文は Resource が `*` で Action がその API の名前だけ。ARN は手で書かず、AppStack がタスクロールの
 * 権限と同じ参照から EndpointAccessScope を作って渡す（BR1.3）。
 *
 * ここは CDK の構成部品を作らない純粋な関数だけを置く（test/endpoint-policies.test.ts）。
 * ポリシーを付けるのは ClosedNetwork.restrictEndpoints。
 */

/**
 * リソースを指定できない一覧の API（タスクロールの `resources: ['*']` の文、U9 NFR7.16）。
 * AppStack.grantTaskRole とエンドポイントのポリシーの両方がこの定数を使う（BR1.3）。並びは
 * タスクロールの文のまま（公開構成のテンプレートを変えない、BR5.2）。
 */
export const TASK_LIST_ACTIONS: readonly string[] = Object.freeze([
  'dynamodb:ListTables',
  'bedrock:ListFoundationModels',
  'bedrock:ListInferenceProfiles',
  'bedrock:ListModelInvocationJobs',
  'bedrock-agentcore:ListMemories',
]);

/** 実行ロールの一覧の API（CDK が ECR のイメージの取得に付ける。Resource は `*`）。 */
export const EXECUTION_LIST_ACTIONS: readonly string[] = Object.freeze(['ecr:GetAuthorizationToken']);

/**
 * アプリが呼ぶ、IAM で認可されない API（BR1.9）。ロールに文がないため、ここに別に持つ。
 * 今は cognito-idp:RevokeToken だけ（src/services/cognito/cognito_auth_service.py の revoke。
 * 要求はクライアントの ID と秘密で認可される）。
 */
export const UNSIGNED_ACTIONS: readonly string[] = Object.freeze(['cognito-idp:RevokeToken']);

/** ポリシーで絞るエンドポイント（ゲートウェイ型 2 とアプリが使うインターフェイス型 10、BR1.1）。 */
export type RestrictedEndpointService =
  | 's3'
  | 'dynamodb'
  | 'ecr.api'
  | 'ecr.dkr'
  | 'logs'
  | 'secretsmanager'
  | 'cognito-idp'
  | 'bedrock-runtime'
  | 'bedrock'
  | 'bedrock-agent-runtime'
  | 'bedrock-agentcore'
  | 'bedrock-agentcore-control';

/**
 * エンドポイントのポリシーで許すアクセス先（entities.md の EndpointAccessScope）。
 *
 * 一覧の API と署名のない API は、ロールと同じ定数（TASK_LIST_ACTIONS・EXECUTION_LIST_ACTIONS・
 * UNSIGNED_ACTIONS）から取るため、ここには持たない。
 */
export interface EndpointAccessScope {
  /** アプリのアップロード用のバケット（オブジェクトは <ARN>/*）。 */
  readonly bucketArn: string;
  /** ECR がイメージのレイヤーを置く AWS 管理のバケット（ecrLayerBucketArn）。読み取りだけ（BR1.5）。 */
  readonly ecrLayerBucketArn: string;
  /** タスクロールが読み書きするテーブルすべて（GSI は <ARN>/index/* を足す）。 */
  readonly tableArns: readonly string[];
  readonly userPoolArn: string;
  /** タスクの起動時に実行ロールが読む秘密すべて。 */
  readonly secretArns: readonly string[];
  /** アプリのロググループの ARN（ロググループの ARN と、ログストリームを含む <ARN>:* の両方）。 */
  readonly logGroupArns: readonly string[];
  readonly repositoryArn: string;
  /** この Stage の AgentCore Memory（子は <ARN>/* を足す）。 */
  readonly memoryArn: string;
  /** BedrockArns の一覧そのもの（BR1.6）。 */
  readonly inferenceArns: readonly string[];
  /** このアカウント・リージョンの knowledge-base/*（タスクロールと同じ）。 */
  readonly knowledgeBaseArn: string;
  /** このアカウント・リージョンの model-invocation-job/*（タスクロールと同じ）。 */
  readonly batchJobArn: string;
  /**
   * バッチ推論のジョブに渡すロール（BedrockBatchRole）。CreateModelInvocationJob の iam:PassRole は、
   * bedrock のエンドポイントのポリシーでも評価される（BR1.10）。
   */
  readonly batchRoleArn: string;
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

const bedrockControl = TASK_LIST_ACTIONS.filter((action) => action.startsWith('bedrock:'));
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

/** functional-spec.md §3 の表（S3 のレイヤーのバケットの文は endpointPolicyStatements で足す）。 */
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
  bedrock: {
    prefix: 'bedrock',
    resources: (s) => [...s.inferenceArns, s.batchJobArn],
    listActions: bedrockControl,
  },
  'bedrock-agent-runtime': { prefix: 'bedrock', resources: (s) => [s.knowledgeBaseArn] },
  'bedrock-agentcore': agentCore,
  'bedrock-agentcore-control': agentCore,
});

/** The endpoints that get a policy (BR1.1); the three SSM endpoints are not in it (BR1.7). */
export const RESTRICTED_ENDPOINT_SERVICES: readonly RestrictedEndpointService[] = Object.freeze(
  Object.keys(POLICY_SHAPES) as RestrictedEndpointService[],
);

/**
 * The statements of one endpoint's policy (functional-spec.md §3): `<prefix>:*` on the app's
 * resources, the list or unsigned APIs on `*`, for S3 `s3:GetObject` on the ECR layer bucket, and
 * for Bedrock `iam:PassRole` on the batch inference role.
 * Every statement allows any principal (BR1.2).
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
    // BR1.5: タスクがイメージのレイヤーを取得するため（読み取りだけ）
    statements.push(allow(['s3:GetObject'], [`${scope.ecrLayerBucketArn}/*`]));
  }
  if (service === 'bedrock') {
    // BR1.10: バッチ推論のジョブの作成でロールを渡す。iam:PassRole もエンドポイントのポリシーの対象になる
    // （AWS Security Blog「How to use the PassRole permission with IAM roles」）。渡せるのはこのロールだけ
    statements.push(allow(['iam:PassRole'], [scope.batchRoleArn]));
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
