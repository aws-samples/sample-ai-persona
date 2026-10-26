import { ClosedStageParameters, DeploymentMode } from '../parameters';

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

/** 閉域のネットワークと入口の設定（検査済みの ClosedStageParameters）。 */
export type ClosedNetworkSettings = Readonly<ClosedStageParameters>;

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
}

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
}

/** The Nemotron key the app has always used (src/models/nemotron_dataset.py). */
export const DEFAULT_NEMOTRON_DATASET_KEY = 'persona-dataset/nemotron-personas-japan.parquet';
/** Places of other files in the bucket (custom persona data, import temp files). */
const RESERVED_KEY_PREFIXES = ['persona-dataset/custom/', 'persona-dataset/temp/'];
/** The ASCII part of the app's runtime S3 URI check (SurveyBatchService). */
const KEY_CHARACTERS = /^[A-Za-z0-9._\-/]+$/;

/** The profile of the public Stage (also the default of AppStack). */
export const PUBLIC_PROFILE: ResolvedProfile = Object.freeze({
  deploymentMode: 'public',
  authMode: 'oidc',
  createCognitoDomain: true,
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
  if (deploymentMode === 'public') {
    return PUBLIC_PROFILE;
  }
  if (!closed) {
    throw new Error(
      '閉域構成のパラメータ（closedCertificateArn・closedDomainName など）が渡されていない',
    );
  }
  const closedNetwork = validateClosedParameters(closed, region);
  return {
    deploymentMode: 'closed',
    authMode: 'admin_api',
    createCognitoDomain: false,
    appBaseUrl: `https://${closedNetwork.domainName}`,
    closedNetwork,
    closedFeatureEnvironment: resolveClosedFeatures(input, closed),
  };
}

/**
 * The per-feature values of the closed task (U3, BR5.1, BR5.3〜BR5.5).
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
  };
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
  for (const cidr of closed.allowedCidrs) {
    if (!isIpv4Cidr(cidr)) {
      throw new Error(`closedAllowedCidrs の "${cidr}" は IPv4 の CIDR（例 10.0.0.0/16）ではない`);
    }
  }
  if (closed.allowedCidrs.length === 0 && !closed.createTestEnvironment) {
    throw new Error(
      '閉域の ALB に届く経路がない: 許可する CIDR（closedAllowedCidrs）か、' +
        '検証用 EC2（closedCreateTestEnvironment=true）のどちらかが要る',
    );
  }
  return Object.freeze({
    vpcCidr: closed.vpcCidr,
    certificateArn: closed.certificateArn,
    domainName: closed.domainName,
    allowedCidrs: [...closed.allowedCidrs],
    createTestEnvironment: closed.createTestEnvironment,
  });
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
