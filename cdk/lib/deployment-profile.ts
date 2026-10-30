import { ClosedStageParameters, DeploymentMode, InferenceScope } from '../parameters';

/**
 * DeploymentProfile（U1 閉域、ADR-002、BR1.1〜BR1.8）。
 *
 * Stage のモード（public / closed）と閉域のパラメータから、Stage ごとの設定（ResolvedProfile）を
 * 作る純粋な関数を置く。CDK の構成部品には依存しない（test/deployment-profile.test.ts だけで
 * 確かめられる）。Data・App スタックと構成部品は ResolvedProfile だけを見て組み立てを変える。
 * 値の誤りは、どのパラメータの何が誤りかを示す Error で synth を止める。
 */

/** アプリのログイン方式（環境変数 AUTH_MODE）。 */
export type AuthMode = 'oidc' | 'admin_api';

/** インターフェイス型エンドポイントを置く AZ の数（閉域対応 U2 BR2.1）。 */
export type EndpointAzCount = 1 | 2;

/** 既定の AZ の数（BR2.1）。closedDevParameter だけ 1 にする（BR2.2）。 */
export const DEFAULT_ENDPOINT_AZ_COUNT: EndpointAzCount = 2;

/** 検査済みの Resolver Inbound Endpoint の設定（閉域対応 U2 BR3.1〜BR3.3）。 */
export interface ResolverSettings {
  /** 社内 DNS の CIDR（1 つ以上の IPv4 CIDR。接頭辞の長さ 0 は含まない）。 */
  readonly allowedCidrs: readonly string[];
}

/**
 * 閉域のネットワークと入口の設定（検査済みの ClosedStageParameters、entities.md の
 * ClosedNetworkSettings）。ClosedNetwork と AppStack はこれだけを見る。
 */
export interface ClosedNetworkSettings {
  readonly vpcCidr: string;
  readonly certificateArn: string;
  readonly domainName: string;
  readonly allowedCidrs: readonly string[];
  readonly createTestEnvironment: boolean;
  /** 閉域対応 U2（BR2.1）。 */
  readonly endpointAzCount: EndpointAzCount;
  /** Resolver を作るときだけある（閉域対応 U2 BR3.1）。 */
  readonly resolver?: ResolverSettings;
}

export interface ResolvedProfile {
  readonly deploymentMode: DeploymentMode;
  readonly authMode: AuthMode;
  /** Cognito の Managed Login のドメインを作るか（closed では作らない）。 */
  readonly createCognitoDomain: boolean;
  /** closed の APP_BASE_URL（https://<FQDN>）。public は CloudFront のドメインから AppStack が作る。 */
  readonly appBaseUrl?: string;
  /** closed のときだけある。 */
  readonly closedNetwork?: ClosedNetworkSettings;
  /**
   * closed のタスクにだけ足す機能ごとの値（U3 閉域、BR5.1）。アプリには「閉域かどうか」を
   * 渡さず、この値で Nemotron の入手先と Data Agent の可否を変える（ADR-002）。
   */
  readonly closedFeatureEnvironment?: Readonly<Record<string, string>>;
  /** 推論の範囲（U5、BR1.1）。jp のときだけタスクに INFERENCE_SCOPE=jp を渡す（BR1.7）。 */
  readonly inferenceScope: InferenceScope;
  /** タスクに渡す 3 つのモデル ID（明示した値か、範囲の既定。BR1.5、BR1.6）。 */
  readonly modelIds: Readonly<ModelIds>;
  /** パラメータで明示したモデル ID だけ（BedrockArns が呼ぶ権限を足す、BR2.3）。 */
  readonly explicitModelIds: Readonly<Partial<ModelIds>>;
}

/** タスクに渡す 3 つのモデル ID（AppParameter の名前のまま）。 */
export interface ModelIds {
  /** ペルソナの生成・インサイト・レポート（BEDROCK_MODEL_ID）。 */
  bedrockModelId: string;
  /** 議論・インタビューの既定（AGENT_MODEL_ID）。 */
  agentModelId: string;
  /** アンケートのバッチ推論（BATCH_INFERENCE_MODEL_ID）。 */
  batchInferenceModelId: string;
}

const MODEL_ID_NAMES: readonly (keyof ModelIds)[] = ['bedrockModelId', 'agentModelId', 'batchInferenceModelId'];

/**
 * 範囲ごとの既定のモデル ID（U5 BR1.6 と計画の「設計からの変更」）。src/models/model_registry.py の
 * SCOPE_DEFAULTS と同じ値（BR4.6。pytest と jest の両方に同じ値を書いて固定する）。jp のバッチ推論が
 * Haiku 4.5 なのは、Haiku 5.5 がバッチ推論の対応表にないため。
 */
export const SCOPE_DEFAULTS: Readonly<Record<InferenceScope, Readonly<ModelIds>>> = Object.freeze({
  global: Object.freeze({
    bedrockModelId: 'global.anthropic.claude-sonnet-5-5',
    agentModelId: 'global.anthropic.claude-haiku-5-5',
    batchInferenceModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  }),
  jp: Object.freeze({
    bedrockModelId: 'jp.anthropic.claude-haiku-5-5',
    agentModelId: 'jp.anthropic.claude-haiku-5-5',
    batchInferenceModelId: 'jp.anthropic.claude-haiku-4-5-20251001-v1:0',
  }),
});

/**
 * 追加のモデル（Bedrock Mantle 経由で米国のリージョンを呼ぶ）の ID。ModelRegistry の
 * requires_mantle のモデルと同じ（BR4.6）。jp では明示もできない（BR1.4）。
 */
export const ADDITIONAL_MODEL_IDS: readonly string[] = Object.freeze([
  'openai.gpt-5.6-terra',
  'openai.gpt-5.6-luna',
  'google.gemma-4-31b',
]);

/** 国内の推論プロファイルを呼べるリージョン（jp の Stack はこのどちらかに置く、BR1.3）。 */
export const JP_REGIONS: readonly string[] = Object.freeze(['ap-northeast-1', 'ap-northeast-3']);

export interface DeploymentProfileInput {
  /** Stage を作る側が決めたモード（コンテキストの値ではない）。 */
  deploymentMode: string;
  /** Stage のリージョン（証明書の ARN のリージョンと照らす）。 */
  region: string;
  closed?: ClosedStageParameters;
  enableMcpGateway?: boolean;
  /** 追加のモデル（Mantle 系）。閉域では true にできない（U3 BR5.4）。 */
  enableAdditionalPersonaModels?: boolean;
  /** Data Agent の Runtime ARN。閉域では渡せない（U3 BR5.5）。 */
  dataAgentRuntimeArn?: string;
  /** 推論の範囲（コンテキスト inferenceScope か AppParameter.inferenceScope。U5 BR1.1）。省略は global。 */
  inferenceScope?: string;
  /** 明示したモデル ID（省略・空・空白は範囲の既定、BR1.5）。 */
  bedrockModelId?: string;
  agentModelId?: string;
  batchInferenceModelId?: string;
}

/** The Nemotron key the app has always used (src/models/nemotron_dataset.py). */
export const DEFAULT_NEMOTRON_DATASET_KEY = 'persona-dataset/nemotron-personas-japan.parquet';
/** Places of other files in the bucket (custom persona data, import temp files). */
const RESERVED_KEY_PREFIXES = ['persona-dataset/custom/', 'persona-dataset/temp/'];
/** The ASCII part of the app's runtime S3 URI check (SurveyBatchService). */
const KEY_CHARACTERS = /^[A-Za-z0-9._\-/]+$/;

/** The profile of the public Stage with the global scope and no explicit model IDs. */
export const PUBLIC_PROFILE: ResolvedProfile = Object.freeze({
  deploymentMode: 'public',
  authMode: 'oidc',
  createCognitoDomain: true,
  inferenceScope: 'global',
  modelIds: SCOPE_DEFAULTS.global,
  explicitModelIds: Object.freeze({}),
});

const MCP_GATEWAY_ERROR =
  'MCP Gateway は後続リリースで提供（enableMcpGateway=true は v1.0.0 では使えません）';

/**
 * Resolve the per-Stage settings from the mode and the closed-network parameters (BR1.3〜BR1.8).
 *
 * Throws an Error that names the offending parameter when a value is invalid.
 */
export function resolveDeploymentProfile(input: DeploymentProfileInput): ResolvedProfile {
  const { deploymentMode, region, closed, enableMcpGateway } = input;
  if (deploymentMode !== 'public' && deploymentMode !== 'closed') {
    throw new Error(`deploymentMode must be "public" or "closed", got "${deploymentMode}"`);
  }
  if (enableMcpGateway) {
    throw new Error(MCP_GATEWAY_ERROR);
  }
  const inferenceScope = resolveInferenceScope(input.inferenceScope, region);
  if (deploymentMode === 'public') {
    if (inferenceScope === 'jp' && input.enableAdditionalPersonaModels) {
      throw new Error(
        'inferenceScope=jp では enableAdditionalPersonaModels を使えない' +
          '（追加のモデルは Bedrock Mantle 経由で米国のリージョンを呼ぶ）',
      );
    }
    const inference = resolveInference(input, inferenceScope);
    if (inferenceScope === 'global' && Object.keys(inference.explicitModelIds).length === 0) {
      return PUBLIC_PROFILE;
    }
    return { deploymentMode: 'public', authMode: 'oidc', createCognitoDomain: true, ...inference };
  }
  if (!closed) {
    throw new Error(
      '閉域構成のパラメータ（closedCertificateArn・closedDomainName など）が渡されていない',
    );
  }
  const closedNetwork = validateClosedParameters(closed, region);
  // U3 の検査（閉域で追加のモデルを止める）を先に通す。jp の追加のモデルの ID の検査はその後
  const closedFeatureEnvironment = resolveClosedFeatures(input, closed);
  return {
    deploymentMode: 'closed',
    authMode: 'admin_api',
    createCognitoDomain: false,
    appBaseUrl: `https://${closedNetwork.domainName}`,
    closedNetwork,
    closedFeatureEnvironment,
    ...resolveInference(input, inferenceScope),
  };
}

/**
 * The inference scope (U5, BR1.1〜BR1.3): trimmed and case-insensitive, `global` when omitted.
 * Throws for values other than global and jp, and for jp outside the two domestic regions.
 */
function resolveInferenceScope(value: string | undefined, region: string): InferenceScope {
  const scope = (value ?? '').trim().toLowerCase() || 'global';
  if (scope !== 'global' && scope !== 'jp') {
    throw new Error(`inferenceScope must be "global" or "jp", got "${value}"`);
  }
  if (scope === 'jp' && !JP_REGIONS.includes(region)) {
    throw new Error(
      `inferenceScope=jp は Stack のリージョンが ${JP_REGIONS.join('・')} のときだけ使える` +
        `（リージョン ${region}。国内の推論プロファイルはこの 2 つのリージョンから呼ぶ）`,
    );
  }
  return scope;
}

/**
 * The three model IDs (U5, BR1.4〜BR1.6): an explicit (non-blank) value is used as is, an omitted
 * one is the scope's default. Under jp, an explicit additional (Mantle) model ID is an error.
 */
function resolveInference(
  input: DeploymentProfileInput,
  inferenceScope: InferenceScope,
): Pick<ResolvedProfile, 'inferenceScope' | 'modelIds' | 'explicitModelIds'> {
  const explicit: Partial<ModelIds> = {};
  for (const name of MODEL_ID_NAMES) {
    const value = input[name]?.trim();
    if (!value) {
      continue;
    }
    if (inferenceScope === 'jp' && ADDITIONAL_MODEL_IDS.includes(value)) {
      throw new Error(
        `inferenceScope=jp では ${name} に "${value}" を使えない` +
          '（追加のモデルは Bedrock Mantle 経由で米国のリージョンを呼ぶ）',
      );
    }
    explicit[name] = value;
  }
  return {
    inferenceScope,
    modelIds: Object.freeze({ ...SCOPE_DEFAULTS[inferenceScope], ...explicit }),
    explicitModelIds: Object.freeze(explicit),
  };
}

/**
 * The per-feature values of the closed task (U3, BR5.1, BR5.3〜BR5.5), and the file delivery
 * mode `ASSET_DELIVERY=proxy` (U4, BR5.1).
 *
 * Throws when the closed Stage is given a feature that needs the internet (additional models,
 * the data agent) or a Nemotron key the app cannot use.
 */
function resolveClosedFeatures(
  input: DeploymentProfileInput,
  closed: ClosedStageParameters,
): Readonly<Record<string, string>> {
  if (input.enableAdditionalPersonaModels) {
    throw new Error(
      '閉域構成では enableAdditionalPersonaModels を使えない（追加のモデルは Bedrock Mantle 経由でインターネットに出る）',
    );
  }
  if (input.dataAgentRuntimeArn) {
    throw new Error('閉域構成では dataAgentRuntimeArn を使えない（データ分析エージェントは閉域で止める）');
  }
  const key = closed.nemotronDatasetKey?.trim() || DEFAULT_NEMOTRON_DATASET_KEY;
  const problem = nemotronDatasetKeyProblem(key);
  if (problem) {
    throw new Error(`closedNemotronDatasetKey の "${key}" は使えない: ${problem}`);
  }
  return Object.freeze({
    NEMOTRON_SOURCE: 's3',
    NEMOTRON_DATASET_KEY: key,
    DATA_AGENT_ALLOWED: 'false',
    // 閉域対応 U4（BR5.1）: 5 か所のファイルをアプリ経由で流す。閉域で presigned を選ぶ入力は作らない
    ASSET_DELIVERY: 'proxy',
  });
}

/**
 * Why `key` cannot be the Nemotron Parquet key, or undefined when it can (BR5.3).
 *
 * The same rules as the app's start-up check (src/models/nemotron_dataset.py, BR1.4).
 */
export function nemotronDatasetKeyProblem(key: string): string | undefined {
  if (!KEY_CHARACTERS.test(key)) {
    return '英数字・.・_・-・/ だけにする（空白・+・引用符・全角を使わない）';
  }
  if (key.startsWith('/')) {
    return '先頭に / を付けない';
  }
  if (key.includes('//')) {
    return '// を含めない';
  }
  if (key.split('/').includes('..')) {
    return '.. の区切りを含めない';
  }
  if (!key.endsWith('.parquet')) {
    return '末尾を .parquet にする';
  }
  const reserved = RESERVED_KEY_PREFIXES.find((prefix) => key.startsWith(prefix));
  if (reserved !== undefined) {
    return `${reserved} の下に置かない（カスタムデータと取り込みの一時ファイルの場所）`;
  }
  return undefined;
}

/**
 * Whether the closed Stage should be built (BR1.1): true when both the certificate ARN and the
 * FQDN are given, false when neither is. Throws when only one of them is given.
 */
export function closedStageRequested(
  params: Pick<ClosedStageParameters, 'certificateArn' | 'domainName'>,
): boolean {
  const missing = [
    params.certificateArn ? undefined : 'closedCertificateArn',
    params.domainName ? undefined : 'closedDomainName',
  ].filter((name): name is string => name !== undefined);
  if (missing.length === 2) {
    return false;
  }
  if (missing.length === 1) {
    throw new Error(`閉域構成には証明書の ARN と FQDN の両方が要る（足りないもの: ${missing[0]}）`);
  }
  return true;
}

/**
 * Overlay the CDK context (`-c closedCertificateArn=...` etc.) on the closed-network defaults.
 *
 * Context values given with `-c` are strings: `closedAllowedCidrs` is comma-separated and
 * `closedCreateTestEnvironment` is `true` or `false`.
 */
export function mergeClosedStageContext(
  base: ClosedStageParameters,
  read: (key: string) => unknown,
): ClosedStageParameters {
  const text = (key: string, fallback: string): string => {
    const value = read(key);
    return value === undefined || value === null ? fallback : String(value).trim();
  };
  const nemotronDatasetKey = read('closedNemotronDatasetKey');
  const endpointAzCount = read('closedEndpointAzCount');
  const createResolverEndpoint = read('closedCreateResolverEndpoint');
  const resolverAllowedCidrs = read('closedResolverAllowedCidrs');
  return {
    certificateArn: text('closedCertificateArn', base.certificateArn),
    domainName: text('closedDomainName', base.domainName),
    allowedCidrs: parseCidrList(read('closedAllowedCidrs'), base.allowedCidrs),
    createTestEnvironment: parseBoolean(
      'closedCreateTestEnvironment',
      read('closedCreateTestEnvironment'),
      base.createTestEnvironment,
    ),
    vpcCidr: text('closedVpcCidr', base.vpcCidr),
    // U3: only when given (the profile falls back to the default key)
    ...(nemotronDatasetKey !== undefined && nemotronDatasetKey !== null
      ? { nemotronDatasetKey: String(nemotronDatasetKey).trim() }
      : base.nemotronDatasetKey !== undefined
        ? { nemotronDatasetKey: base.nemotronDatasetKey }
        : {}),
    // U2: only when given or set in parameters.ts (the profile applies the defaults)
    ...(isGiven(endpointAzCount)
      ? { endpointAzCount: parseAzCount('closedEndpointAzCount', endpointAzCount) }
      : base.endpointAzCount !== undefined
        ? { endpointAzCount: base.endpointAzCount }
        : {}),
    ...(isGiven(createResolverEndpoint) || base.createResolverEndpoint !== undefined
      ? {
        createResolverEndpoint: parseBoolean(
          'closedCreateResolverEndpoint',
          createResolverEndpoint,
          base.createResolverEndpoint ?? false,
        ),
      }
      : {}),
    ...(isGiven(resolverAllowedCidrs) || base.resolverAllowedCidrs !== undefined
      ? { resolverAllowedCidrs: parseCidrList(resolverAllowedCidrs, base.resolverAllowedCidrs ?? []) }
      : {}),
  };
}

function isGiven(value: unknown): boolean {
  return value !== undefined && value !== null;
}

/**
 * The endpoint AZ count from a context string (`"1"` or `"2"`, BR2.1). Throws with the parameter
 * name and the value received for anything else (`"0"`, `"3"`, `"two"`, empty).
 */
function parseAzCount(name: string, value: unknown): number {
  const text = String(value).trim();
  if (text !== '1' && text !== '2') {
    throw new Error(`${name} は 1 か 2 にする: "${String(value)}"`);
  }
  return Number(text);
}

function parseCidrList(value: unknown, fallback: string[]): string[] {
  if (value === undefined || value === null) {
    return [...fallback];
  }
  const items = Array.isArray(value) ? value.map(String) : String(value).split(',');
  return items.map((item) => item.trim()).filter((item) => item.length > 0);
}

function parseBoolean(name: string, value: unknown, fallback: boolean): boolean {
  if (value === undefined || value === null) {
    return fallback;
  }
  if (typeof value === 'boolean') {
    return value;
  }
  if (value === 'true' || value === 'false') {
    return value === 'true';
  }
  throw new Error(`${name} は true か false にする: "${String(value)}"`);
}

function validateClosedParameters(closed: ClosedStageParameters, region: string): ClosedNetworkSettings {
  validateDomainName(closed.domainName);
  validateCertificateArn(closed.certificateArn, region);
  if (!isIpv4Cidr(closed.vpcCidr)) {
    throw new Error(`closedVpcCidr の "${closed.vpcCidr}" は IPv4 の CIDR（例 10.0.0.0/16）ではない`);
  }
  requireIpv4Cidrs('closedAllowedCidrs', closed.allowedCidrs);
  if (closed.allowedCidrs.length === 0 && !closed.createTestEnvironment) {
    throw new Error(
      '閉域の ALB に届く経路がない: 許可する CIDR（closedAllowedCidrs）か、' +
        '検証用 EC2（closedCreateTestEnvironment=true）のどちらかが要る',
    );
  }
  const resolver = validateResolver(closed);
  return Object.freeze({
    vpcCidr: closed.vpcCidr,
    certificateArn: closed.certificateArn,
    domainName: closed.domainName,
    allowedCidrs: [...closed.allowedCidrs],
    createTestEnvironment: closed.createTestEnvironment,
    endpointAzCount: validateAzCount(closed.endpointAzCount),
    ...(resolver ? { resolver } : {}),
  });
}

/** BR2.1: 1 or 2, 2 when omitted. */
function validateAzCount(count: number | undefined): EndpointAzCount {
  if (count === undefined) {
    return DEFAULT_ENDPOINT_AZ_COUNT;
  }
  if (count !== 1 && count !== 2) {
    throw new Error(`closedEndpointAzCount は 1 か 2 にする: "${String(count)}"`);
  }
  return count;
}

/**
 * BR3.1〜BR3.3: the Resolver settings when it is requested, undefined when it is not.
 * Throws when it is requested without CIDRs, when CIDRs are given without the request,
 * and for a CIDR that is not IPv4 or whose prefix length is 0.
 */
function validateResolver(closed: ClosedStageParameters): ResolverSettings | undefined {
  const cidrs = closed.resolverAllowedCidrs ?? [];
  if (!closed.createResolverEndpoint) {
    if (cidrs.length > 0) {
      throw new Error(
        `closedResolverAllowedCidrs（${cidrs.join(',')}）は Resolver を作るときだけ使う` +
          '（closedCreateResolverEndpoint=true を付け忘れていないか確かめる）',
      );
    }
    return undefined;
  }
  if (cidrs.length === 0) {
    throw new Error(
      'Resolver Inbound Endpoint を作るには、社内 DNS の CIDR（closedResolverAllowedCidrs）が 1 つ以上要る',
    );
  }
  requireIpv4Cidrs('closedResolverAllowedCidrs', cidrs, { rejectZeroPrefix: true });
  return Object.freeze({ allowedCidrs: Object.freeze([...cidrs]) });
}

/**
 * Throws, naming the parameter, for the first value that is not an IPv4 CIDR (and, with
 * `rejectZeroPrefix`, for a prefix length of 0, which accepts every address).
 */
function requireIpv4Cidrs(
  name: string,
  cidrs: readonly string[],
  options: { rejectZeroPrefix?: boolean } = {},
): void {
  for (const cidr of cidrs) {
    if (!isIpv4Cidr(cidr)) {
      throw new Error(`${name} の "${cidr}" は IPv4 の CIDR（例 10.0.0.0/16）ではない`);
    }
    // 接頭辞は数で比べる（"/00" も 0）
    if (options.rejectZeroPrefix && Number(cidr.split('/')[1]) === 0) {
      throw new Error(`${name} の "${cidr}" は使えない: 接頭辞の長さ /0 はどこからでも受け入れる`);
    }
  }
}

const DNS_LABEL = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
const MAX_DOMAIN_LENGTH = 253;

/** BR1.6: a bare FQDN (no scheme, path, upper case or trailing dot; two labels or more). */
function validateDomainName(domainName: string): void {
  const fail = (reason: string) => {
    throw new Error(`closedDomainName の "${domainName}" は FQDN として使えない: ${reason}`);
  };
  if (domainName.includes('://')) {
    fail('スキーム（https:// など）を付けない');
  }
  if (domainName.includes('/')) {
    fail('パスを付けない');
  }
  if (/[A-Z]/.test(domainName)) {
    fail('大文字を使わない（英小文字にする）');
  }
  if (domainName.endsWith('.')) {
    fail('末尾のドットを付けない');
  }
  if (domainName.length > MAX_DOMAIN_LENGTH) {
    fail(`${MAX_DOMAIN_LENGTH} 文字以内にする`);
  }
  const labels = domainName.split('.');
  if (labels.length < 2) {
    fail('ラベルを 2 つ以上にする（例 ai-persona.example.internal）');
  }
  const bad = labels.find((label) => !DNS_LABEL.test(label));
  if (bad !== undefined) {
    fail(`ラベル "${bad}" は英小文字・数字・ハイフンの 1〜63 文字で、先頭と末尾はハイフン以外にする`);
  }
}

const ACM_CERTIFICATE_ARN = /^arn:aws(-cn|-us-gov)?:acm:([a-z0-9-]+):(\d{12}):certificate\/[A-Za-z0-9-]+$/;

/** BR1.6: an ACM certificate ARN in the Stage's region. */
function validateCertificateArn(certificateArn: string, region: string): void {
  const match = ACM_CERTIFICATE_ARN.exec(certificateArn);
  if (!match) {
    throw new Error(
      `closedCertificateArn の "${certificateArn}" は ACM の証明書の ARN ` +
        '（arn:aws:acm:<リージョン>:<アカウント>:certificate/<ID>）ではない',
    );
  }
  const certificateRegion = match[2];
  if (certificateRegion !== region) {
    throw new Error(
      `closedCertificateArn のリージョン ${certificateRegion} が Stage のリージョン ${region} と違う` +
        '（ALB と同じリージョンの ACM にインポートした証明書が要る）',
    );
  }
}

function isIpv4Cidr(value: string): boolean {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/.exec(value);
  if (!match) {
    return false;
  }
  const octets = match.slice(1, 5).map(Number);
  const prefix = Number(match[5]);
  return octets.every((octet) => octet <= 255) && prefix <= 32;
}
