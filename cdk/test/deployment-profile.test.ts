import {
  ADDITIONAL_MODEL_IDS,
  closedStageRequested,
  mergeClosedStageContext,
  PUBLIC_PROFILE,
  resolveDeploymentProfile,
  SCOPE_DEFAULTS,
} from '../lib/deployment-profile';
import { ClosedStageParameters, closedDevParameter, devParameter, prodParameter } from '../parameters';

// 範囲ごとの既定（U5 BR1.6 と計画の「設計からの変更」）。tests/unit/test_model_registry.py にも
// 同じ値を書く（BR4.6）。どちらかを変えたら両方のテストが落ちる
const GLOBAL_DEFAULTS = {
  bedrockModelId: 'global.anthropic.claude-sonnet-5',
  agentModelId: 'global.anthropic.claude-haiku-5-5',
  batchInferenceModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
};
const JP_DEFAULTS = {
  bedrockModelId: 'jp.anthropic.claude-haiku-5-5',
  agentModelId: 'jp.anthropic.claude-haiku-5-5',
  batchInferenceModelId: 'jp.anthropic.claude-haiku-4-5-20251001-v1:0',
};
const MANTLE_MODEL_IDS = ['openai.gpt-5.6-terra', 'openai.gpt-5.6-luna', 'google.gemma-4-31b'];

/**
 * DeploymentProfile（U1 閉域、ADR-002、BR1.1〜BR1.8）の単体テスト。
 *
 * resolveDeploymentProfile() は CDK の構成部品に依存しない純粋な関数で、Stage を作らずに
 * モードの解決と閉域のパラメータの検査を確かめる。
 */

const REGION = 'ap-northeast-1';
const CERTIFICATE_ARN =
  'arn:aws:acm:ap-northeast-1:123456789012:certificate/00000000-0000-0000-0000-000000000000';
const DOMAIN_NAME = 'ai-persona.closed.example';

function closed(overrides: Partial<ClosedStageParameters> = {}): ClosedStageParameters {
  return {
    certificateArn: CERTIFICATE_ARN,
    domainName: DOMAIN_NAME,
    allowedCidrs: ['10.10.0.0/16'],
    createTestEnvironment: false,
    vpcCidr: '10.0.0.0/16',
    ...overrides,
  };
}

const resolveClosed = (overrides: Partial<ClosedStageParameters> = {}) =>
  resolveDeploymentProfile({ deploymentMode: 'closed', region: REGION, closed: closed(overrides) });

describe('resolveDeploymentProfile: モードの解決（BR1.3、BR1.5）', () => {
  test('public は oidc・ドメインを作る・閉域の設定なし', () => {
    const profile = resolveDeploymentProfile({ deploymentMode: 'public', region: REGION });
    expect(profile).toEqual({
      deploymentMode: 'public',
      authMode: 'oidc',
      createCognitoDomain: true,
      // U5: 範囲は global、モデル ID は global の既定、明示した値なし
      inferenceScope: 'global',
      modelIds: GLOBAL_DEFAULTS,
      explicitModelIds: {},
    });
  });

  test('closed は admin_api・ドメインを作らない・APP_BASE_URL は https://<FQDN>', () => {
    const profile = resolveClosed({ createTestEnvironment: true, allowedCidrs: [] });
    expect(profile.deploymentMode).toBe('closed');
    expect(profile.authMode).toBe('admin_api');
    expect(profile.createCognitoDomain).toBe(false);
    expect(profile.appBaseUrl).toBe(`https://${DOMAIN_NAME}`);
    expect(profile.closedNetwork).toEqual({
      vpcCidr: '10.0.0.0/16',
      certificateArn: CERTIFICATE_ARN,
      domainName: DOMAIN_NAME,
      allowedCidrs: [],
      createTestEnvironment: true,
      // 閉域対応 U2（BR2.1、BR3.1）: AZ の数は既定 2、Resolver は既定で作らない
      endpointAzCount: 2,
    });
  });

  test('closed で閉域のパラメータがなければエラー', () => {
    expect(() => resolveDeploymentProfile({ deploymentMode: 'closed', region: REGION })).toThrow(
      /閉域構成のパラメータ/,
    );
  });
});

describe('resolveDeploymentProfile: 検査（BR1.4、BR1.6〜BR1.8）', () => {
  test.each(['private', '', 'CLOSED'])('deploymentMode=%p は public・closed 以外としてエラー（BR1.4）', (mode) => {
    expect(() => resolveDeploymentProfile({ deploymentMode: mode, region: REGION })).toThrow(
      `deploymentMode must be "public" or "closed", got "${mode}"`,
    );
  });

  test.each([
    ['https://ai-persona.closed.example', /closedDomainName.*スキーム/],
    ['ai-persona.closed.example/login', /closedDomainName.*パス/],
    ['AI-Persona.closed.example', /closedDomainName.*大文字/],
    ['ai-persona.closed.example.', /closedDomainName.*末尾のドット/],
    ['localhost', /closedDomainName.*ラベル/],
    ['-bad.closed.example', /closedDomainName.*ラベル/],
  ])('FQDN の形の誤り %p はエラー（BR1.6）', (domainName, message) => {
    expect(() => resolveClosed({ domainName })).toThrow(message);
  });

  test.each([
    ['arn:aws:iam::123456789012:server-certificate/x', /closedCertificateArn.*ACM/],
    ['not-an-arn', /closedCertificateArn.*ACM/],
    [
      'arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000',
      /closedCertificateArn.*us-east-1.*ap-northeast-1/,
    ],
  ])('証明書の ARN の形・リージョンの誤り %p はエラー（BR1.6）', (certificateArn, message) => {
    expect(() => resolveClosed({ certificateArn })).toThrow(message);
  });

  test('許可 CIDR が空で検証用 EC2 も作らないとエラー（BR1.7）', () => {
    expect(() => resolveClosed({ allowedCidrs: [], createTestEnvironment: false })).toThrow(
      /closedAllowedCidrs.*closedCreateTestEnvironment/,
    );
  });

  test('許可 CIDR が空でも検証用 EC2 を作るなら通る（BR1.7）', () => {
    expect(() => resolveClosed({ allowedCidrs: [], createTestEnvironment: true })).not.toThrow();
  });

  test.each(['10.10.0.0', '10.10.0.0/33', '300.1.1.1/32', '::/0', 'abc'])(
    '誤った CIDR %p はエラー（BR1.7）',
    (cidr) => {
      expect(() => resolveClosed({ allowedCidrs: [cidr] })).toThrow(new RegExp(`closedAllowedCidrs.*${cidr.replace(/[./]/g, '\\$&')}`));
    },
  );

  test('VPC の CIDR の誤りもエラー', () => {
    expect(() => resolveClosed({ vpcCidr: '10.0.0.0' })).toThrow(/closedVpcCidr/);
  });

  test.each(['public', 'closed'])('enableMcpGateway=true は %s でもエラー（BR1.8、今の文言）', (mode) => {
    expect(() =>
      resolveDeploymentProfile({
        deploymentMode: mode,
        region: REGION,
        closed: mode === 'closed' ? closed() : undefined,
        enableMcpGateway: true,
      }),
    ).toThrow('MCP Gateway は後続リリースで提供（enableMcpGateway=true は v1.0.0 では使えません）');
  });
});

describe('closedStageRequested: 閉域の Stage を作るか（BR1.1）', () => {
  test('証明書の ARN と FQDN の両方があれば作る', () => {
    expect(closedStageRequested(closed())).toBe(true);
  });

  test('両方なければ作らない（CI の synth と同じ）', () => {
    expect(closedStageRequested(closed({ certificateArn: '', domainName: '' }))).toBe(false);
  });

  test.each([
    [{ certificateArn: '' }, 'closedCertificateArn'],
    [{ domainName: '' }, 'closedDomainName'],
  ])('片方だけ %p ならエラーで、欠けたパラメータ名を示す', (overrides, missing) => {
    expect(() => closedStageRequested(closed(overrides))).toThrow(
      new RegExp(`閉域構成には証明書の ARN と FQDN の両方が要る.*${missing}`),
    );
  });
});

describe('mergeClosedStageContext: コンテキストを閉域の既定に重ねる', () => {
  const base = closedDevParameter.closed as ClosedStageParameters;

  test('closedDevParameter の既定は空の証明書・FQDN、空の許可 CIDR、検証用 EC2 なし、10.0.0.0/16、AZ の数 1', () => {
    expect(base).toEqual({
      certificateArn: '',
      domainName: '',
      allowedCidrs: [],
      createTestEnvironment: false,
      vpcCidr: '10.0.0.0/16',
      // 閉域対応 U2（BR2.2）: dev はインターフェイス型エンドポイントを 1 AZ に置く
      endpointAzCount: 1,
    });
    expect(closedDevParameter.envName).toBe('dev-closed');
    expect(closedDevParameter.deploymentMode).toBe('closed');
    // 公開の dev の値（モデル・台数など）はそのまま写す。モデル ID は省略（U5 BR1.5）なので、
    // 解決後の 3 つの ID が同じことを比べる
    const modelIdsOf = (parameter: typeof devParameter) => ({
      inferenceScope: parameter.inferenceScope,
      bedrockModelId: parameter.bedrockModelId,
      agentModelId: parameter.agentModelId,
      batchInferenceModelId: parameter.batchInferenceModelId,
    });
    const publicIds = resolveDeploymentProfile({
      deploymentMode: 'public',
      region: REGION,
      ...modelIdsOf(devParameter),
    }).modelIds;
    const closedIds = resolveDeploymentProfile({
      deploymentMode: 'closed',
      region: REGION,
      closed: closed(),
      ...modelIdsOf(closedDevParameter),
    }).modelIds;
    expect(closedIds).toEqual(publicIds);
    expect(closedDevParameter.appDesiredCount).toBe(devParameter.appDesiredCount);
    expect(devParameter.envName).toBe('dev');
  });

  test('コンテキストの文字列（カンマ区切りの CIDR、"true"）を型に直して重ねる', () => {
    const context: Record<string, string> = {
      closedCertificateArn: CERTIFICATE_ARN,
      closedDomainName: DOMAIN_NAME,
      closedAllowedCidrs: '10.10.0.0/16, 192.168.0.0/24',
      closedCreateTestEnvironment: 'true',
      closedVpcCidr: '10.1.0.0/16',
    };
    expect(mergeClosedStageContext(base, (key) => context[key])).toEqual({
      certificateArn: CERTIFICATE_ARN,
      domainName: DOMAIN_NAME,
      allowedCidrs: ['10.10.0.0/16', '192.168.0.0/24'],
      createTestEnvironment: true,
      vpcCidr: '10.1.0.0/16',
      endpointAzCount: 1,
    });
  });

  test('コンテキストがなければ既定のまま。空の CIDR の文字列は空の一覧', () => {
    expect(mergeClosedStageContext(base, () => undefined)).toEqual(base);
    expect(mergeClosedStageContext(base, (key) => (key === 'closedAllowedCidrs' ? '' : undefined)).allowedCidrs).toEqual([]);
  });

  test('closedCreateTestEnvironment が true・false 以外ならエラー', () => {
    expect(() =>
      mergeClosedStageContext(base, (key) => (key === 'closedCreateTestEnvironment' ? 'yes' : undefined)),
    ).toThrow(/closedCreateTestEnvironment/);
  });

  test('closedNemotronDatasetKey を渡したときだけ nemotronDatasetKey を重ねる（U3）', () => {
    const merged = mergeClosedStageContext(base, (key) =>
      key === 'closedNemotronDatasetKey' ? ' closed/nemotron.parquet ' : undefined,
    );
    expect(merged.nemotronDatasetKey).toBe('closed/nemotron.parquet');
    expect(mergeClosedStageContext(base, () => undefined)).not.toHaveProperty('nemotronDatasetKey');
  });
});

describe('resolveDeploymentProfile: 閉域で使えない機能（U3 BR5.1〜BR5.5）', () => {
  const DEFAULT_KEY = 'persona-dataset/nemotron-personas-japan.parquet';

  test('閉域のタスクに足す環境変数 3 つ（キーは既定）と U4 の配信の方式（BR5.1）', () => {
    expect(resolveClosed().closedFeatureEnvironment).toEqual({
      NEMOTRON_SOURCE: 's3',
      NEMOTRON_DATASET_KEY: DEFAULT_KEY,
      DATA_AGENT_ALLOWED: 'false',
      ASSET_DELIVERY: 'proxy',
    });
  });

  test.each([['closed/nemotron.parquet'], ['persona-dataset/closed/nemotron_v2-1.parquet']])(
    '指定のキー %p をそのまま渡す（BR5.1）',
    (key) => {
      expect(resolveClosed({ nemotronDatasetKey: key }).closedFeatureEnvironment?.NEMOTRON_DATASET_KEY).toBe(key);
    },
  );

  test('空のキーは既定のキー（BR5.3）', () => {
    expect(resolveClosed({ nemotronDatasetKey: '' }).closedFeatureEnvironment?.NEMOTRON_DATASET_KEY).toBe(
      DEFAULT_KEY,
    );
  });

  test.each([
    'my key.parquet',
    'a+b.parquet',
    '/x.parquet',
    'a/../b.parquet',
    'a//b.parquet',
    'x.csv',
    'persona-dataset/custom/x.parquet',
    'persona-dataset/temp/x.parquet',
    'データ.parquet',
  ])('キーの誤り %p で synth を止める（BR5.3）', (key) => {
    expect(() => resolveClosed({ nemotronDatasetKey: key })).toThrow(/closedNemotronDatasetKey/);
  });

  test('閉域で enableAdditionalPersonaModels=true は止める（BR5.4）', () => {
    expect(() =>
      resolveDeploymentProfile({
        deploymentMode: 'closed',
        region: REGION,
        closed: closed(),
        enableAdditionalPersonaModels: true,
      }),
    ).toThrow(/enableAdditionalPersonaModels/);
  });

  test('閉域で dataAgentRuntimeArn を渡したら止める（BR5.5）', () => {
    expect(() =>
      resolveDeploymentProfile({
        deploymentMode: 'closed',
        region: REGION,
        closed: closed(),
        dataAgentRuntimeArn: 'arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/x',
      }),
    ).toThrow(/dataAgentRuntimeArn/);
  });

  test('公開では止めず、環境変数も足さない（BR5.2）', () => {
    const profile = resolveDeploymentProfile({
      deploymentMode: 'public',
      region: REGION,
      enableAdditionalPersonaModels: true,
      dataAgentRuntimeArn: 'arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/x',
    });
    expect(profile).not.toHaveProperty('closedFeatureEnvironment');
  });
});

describe('resolveDeploymentProfile: ファイルの配信の方式（閉域対応 U4 BR5.1、BR5.2）', () => {
  test('閉域のタスクには ASSET_DELIVERY=proxy を渡す（閉域で presigned を選ぶ入力はない）', () => {
    expect(resolveClosed().closedFeatureEnvironment?.ASSET_DELIVERY).toBe('proxy');
    expect(resolveClosed({ nemotronDatasetKey: 'closed/nemotron.parquet' }).closedFeatureEnvironment?.ASSET_DELIVERY).toBe(
      'proxy',
    );
  });

  test('公開には ASSET_DELIVERY を渡さない（アプリの既定 presigned のまま）', () => {
    const profile = resolveDeploymentProfile({ deploymentMode: 'public', region: REGION });
    expect(profile.closedFeatureEnvironment?.ASSET_DELIVERY).toBeUndefined();
  });
});

const resolvePublic = (input: Partial<Parameters<typeof resolveDeploymentProfile>[0]> = {}) =>
  resolveDeploymentProfile({ deploymentMode: 'public', region: REGION, ...input });

describe('resolveDeploymentProfile: 推論の範囲の決定と検査（閉域対応 U5 BR1.1〜BR1.4）', () => {
  test('指定がなければ global（BR1.1）', () => {
    expect(resolvePublic().inferenceScope).toBe('global');
    expect(resolvePublic({ inferenceScope: '' }).inferenceScope).toBe('global');
  });

  test.each([['jp'], ['JP'], [' Jp ']])('%p は jp（大文字・前後の空白を区別しない）', (value) => {
    expect(resolvePublic({ inferenceScope: value }).inferenceScope).toBe('jp');
    expect(resolveClosedWith({ inferenceScope: value }).inferenceScope).toBe('jp');
  });

  test.each([['us'], ['world'], ['japan']])('%p はパラメータの名前と値を示してエラー（BR1.2）', (value) => {
    expect(() => resolvePublic({ inferenceScope: value })).toThrow(
      new RegExp(`inferenceScope.*"${value}"`),
    );
  });

  test('jp は ap-northeast-3 でも使える', () => {
    expect(resolvePublic({ inferenceScope: 'jp', region: 'ap-northeast-3' }).inferenceScope).toBe('jp');
  });

  test('jp でリージョンが us-east-1 ならエラー（BR1.3）', () => {
    expect(() => resolvePublic({ inferenceScope: 'jp', region: 'us-east-1' })).toThrow(
      /inferenceScope=jp.*us-east-1/,
    );
  });

  test('global ならリージョンを問わない', () => {
    expect(resolvePublic({ region: 'us-east-1' }).inferenceScope).toBe('global');
  });

  test('公開の jp と enableAdditionalPersonaModels=true はエラー（BR1.4、Mantle の米国のリージョン）', () => {
    expect(() => resolvePublic({ inferenceScope: 'jp', enableAdditionalPersonaModels: true })).toThrow(
      /inferenceScope=jp.*enableAdditionalPersonaModels.*Mantle/,
    );
  });

  test('閉域の jp と enableAdditionalPersonaModels=true は U3 の文言で止まる', () => {
    expect(() => resolveClosedWith({ inferenceScope: 'jp', enableAdditionalPersonaModels: true })).toThrow(
      /閉域構成では enableAdditionalPersonaModels を使えない/,
    );
  });

  test.each(MANTLE_MODEL_IDS)('jp で追加のモデルの ID %p を明示したらエラー（公開・閉域）', (id) => {
    expect(() => resolvePublic({ inferenceScope: 'jp', agentModelId: id })).toThrow(
      new RegExp(`agentModelId.*"${id}".*Mantle`),
    );
    expect(() => resolveClosedWith({ inferenceScope: 'jp', bedrockModelId: id })).toThrow(
      new RegExp(`bedrockModelId.*"${id}".*Mantle`),
    );
  });

  test('global なら追加のモデルの ID の明示も通す（今のまま）', () => {
    expect(resolvePublic({ agentModelId: 'google.gemma-4-31b' }).modelIds.agentModelId).toBe('google.gemma-4-31b');
  });
});

function resolveClosedWith(input: Partial<Parameters<typeof resolveDeploymentProfile>[0]> = {}) {
  return resolveDeploymentProfile({ deploymentMode: 'closed', region: REGION, closed: closed(), ...input });
}

describe('resolveDeploymentProfile: モデル ID（閉域対応 U5 BR1.5〜BR1.7、BR4.6）', () => {
  test('範囲ごとの既定の表（pytest と同じ値）', () => {
    expect(SCOPE_DEFAULTS).toEqual({ global: GLOBAL_DEFAULTS, jp: JP_DEFAULTS });
  });

  test('追加のモデルの ID は ModelRegistry の requires_mantle と同じ 3 つ', () => {
    expect([...ADDITIONAL_MODEL_IDS].sort()).toEqual([...MANTLE_MODEL_IDS].sort());
  });

  test('省略した値は範囲の既定（公開・閉域）', () => {
    expect(resolvePublic().modelIds).toEqual(GLOBAL_DEFAULTS);
    expect(resolvePublic({ inferenceScope: 'jp' }).modelIds).toEqual(JP_DEFAULTS);
    expect(resolveClosedWith().modelIds).toEqual(GLOBAL_DEFAULTS);
    expect(resolveClosedWith({ inferenceScope: 'jp' }).modelIds).toEqual(JP_DEFAULTS);
  });

  test('明示した値はそのまま使い、明示した値の集まりに残す（jp で global.* も検査しない）', () => {
    const profile = resolvePublic({
      inferenceScope: 'jp',
      bedrockModelId: 'global.anthropic.claude-sonnet-5',
      batchInferenceModelId: ' anthropic.claude-haiku-4-5-20251001-v1:0 ',
    });
    expect(profile.modelIds).toEqual({
      bedrockModelId: 'global.anthropic.claude-sonnet-5',
      agentModelId: JP_DEFAULTS.agentModelId,
      batchInferenceModelId: 'anthropic.claude-haiku-4-5-20251001-v1:0',
    });
    expect(profile.explicitModelIds).toEqual({
      bedrockModelId: 'global.anthropic.claude-sonnet-5',
      batchInferenceModelId: 'anthropic.claude-haiku-4-5-20251001-v1:0',
    });
  });

  test('空・空白だけは省略と同じ', () => {
    const profile = resolvePublic({ inferenceScope: 'jp', agentModelId: '  ', bedrockModelId: '' });
    expect(profile.modelIds).toEqual(JP_DEFAULTS);
    expect(profile.explicitModelIds).toEqual({});
  });

  test('parameters.ts の dev・prod はモデル ID を省略する（明示のままだと jp でも global が残る）', () => {
    for (const parameter of [devParameter, prodParameter, closedDevParameter]) {
      expect(parameter.bedrockModelId).toBeUndefined();
      expect(parameter.agentModelId).toBeUndefined();
      expect(parameter.batchInferenceModelId).toBeUndefined();
    }
  });

  test('global で何も明示しなければ PUBLIC_PROFILE と同じ（公開構成のテンプレートを変えない、BR1.8）', () => {
    expect(resolvePublic()).toEqual(PUBLIC_PROFILE);
  });
});

describe('resolveDeploymentProfile: インターフェイス型エンドポイントの AZ の数（閉域対応 U2 BR2.1）', () => {
  test('省略すると 2', () => {
    expect(resolveClosed().closedNetwork?.endpointAzCount).toBe(2);
  });

  test.each([1, 2])('%p はそのまま使う', (count) => {
    expect(resolveClosed({ endpointAzCount: count }).closedNetwork?.endpointAzCount).toBe(count);
  });

  test.each([0, 3, 1.5, Number.NaN])('%p はパラメータの名前と値を示してエラー', (count) => {
    expect(() => resolveClosed({ endpointAzCount: count })).toThrow(
      new RegExp(`closedEndpointAzCount.*"${String(count).replace('.', '\\.')}"`),
    );
  });
});

describe('resolveDeploymentProfile: Resolver Inbound Endpoint（閉域対応 U2 BR3.1〜BR3.3）', () => {
  const CORPORATE_DNS = ['10.1.0.0/16', '10.2.0.0/16'];

  test('既定では作らない（resolver の設定がない）', () => {
    expect(resolveClosed().closedNetwork).not.toHaveProperty('resolver');
  });

  test('作る指定と社内 DNS の CIDR があれば、設定に入る（BR3.1）', () => {
    const settings = resolveClosed({ createResolverEndpoint: true, resolverAllowedCidrs: CORPORATE_DNS }).closedNetwork;
    expect(settings?.resolver).toEqual({ allowedCidrs: CORPORATE_DNS });
  });

  test('作る指定で CIDR が空ならエラー（BR3.2）', () => {
    expect(() => resolveClosed({ createResolverEndpoint: true, resolverAllowedCidrs: [] })).toThrow(
      /closedResolverAllowedCidrs/,
    );
    expect(() => resolveClosed({ createResolverEndpoint: true })).toThrow(/closedResolverAllowedCidrs/);
  });

  test('作らないのに CIDR を渡したら、closedCreateResolverEndpoint=true の付け忘れを示してエラー（BR3.3）', () => {
    expect(() => resolveClosed({ createResolverEndpoint: false, resolverAllowedCidrs: CORPORATE_DNS })).toThrow(
      /closedResolverAllowedCidrs.*closedCreateResolverEndpoint=true/,
    );
    expect(() => resolveClosed({ resolverAllowedCidrs: CORPORATE_DNS })).toThrow(/closedCreateResolverEndpoint=true/);
  });

  test.each(['10.0.0.0', '10.0.0.0/33', 'fd00::/8', 'abc'])('IPv4 の CIDR でない %p はエラー（BR3.2）', (cidr) => {
    expect(() => resolveClosed({ createResolverEndpoint: true, resolverAllowedCidrs: [cidr] })).toThrow(
      new RegExp(`closedResolverAllowedCidrs.*${cidr.replace(/[./]/g, '\\$&')}.*IPv4`),
    );
  });

  test.each(['0.0.0.0/0', '10.0.0.0/0', '0.0.0.0/00'])('接頭辞の長さが 0 の %p はエラー（どこからでも受け入れるため、BR3.2）', (cidr) => {
    expect(() =>
      resolveClosed({ createResolverEndpoint: true, resolverAllowedCidrs: ['10.1.0.0/16', cidr] }),
    ).toThrow(new RegExp(`closedResolverAllowedCidrs.*${cidr.replace(/[./]/g, '\\$&')}.*/0`));
  });

  test('closedAllowedCidrs の検査の文言は今のまま（パラメータの名前ごと）', () => {
    expect(() => resolveClosed({ allowedCidrs: ['10.0.0.0'] })).toThrow(
      'closedAllowedCidrs の "10.0.0.0" は IPv4 の CIDR（例 10.0.0.0/16）ではない',
    );
  });
});

describe('mergeClosedStageContext: U2 の 3 つのコンテキスト（閉域対応 U2）', () => {
  const base = closed();

  test('文字列を数・真偽値・CIDR の並びにする', () => {
    const context: Record<string, string> = {
      closedEndpointAzCount: '1',
      closedCreateResolverEndpoint: 'true',
      closedResolverAllowedCidrs: '10.1.0.0/16, 10.2.0.0/16',
    };
    expect(mergeClosedStageContext(base, (key) => context[key])).toEqual({
      ...base,
      endpointAzCount: 1,
      createResolverEndpoint: true,
      resolverAllowedCidrs: ['10.1.0.0/16', '10.2.0.0/16'],
    });
  });

  test('コンテキストがなければ 3 つのキーを足さない。parameters.ts の値はそのまま残す', () => {
    expect(mergeClosedStageContext(base, () => undefined)).toEqual(base);
    const withBase = closed({ endpointAzCount: 1, createResolverEndpoint: true, resolverAllowedCidrs: ['10.1.0.0/16'] });
    expect(mergeClosedStageContext(withBase, () => undefined)).toEqual(withBase);
  });

  test('コンテキストの AZ の数が parameters.ts の値より優先する（BR2.1）', () => {
    const merged = mergeClosedStageContext(closed({ endpointAzCount: 1 }), (key) =>
      key === 'closedEndpointAzCount' ? '2' : undefined,
    );
    expect(merged.endpointAzCount).toBe(2);
  });

  test.each(['0', '3', 'two', '', '1.5', ' '])('closedEndpointAzCount の %p は名前と値を示してエラー', (value) => {
    expect(() =>
      mergeClosedStageContext(base, (key) => (key === 'closedEndpointAzCount' ? value : undefined)),
    ).toThrow(new RegExp(`closedEndpointAzCount.*"${value.replace('.', '\\.')}"`));
  });

  test('closedCreateResolverEndpoint が true・false 以外ならエラー（今の parseBoolean と同じ）', () => {
    expect(() =>
      mergeClosedStageContext(base, (key) => (key === 'closedCreateResolverEndpoint' ? 'yes' : undefined)),
    ).toThrow(/closedCreateResolverEndpoint は true か false にする: "yes"/);
  });
});
