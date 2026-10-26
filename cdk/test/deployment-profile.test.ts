import {
  closedStageRequested,
  mergeClosedStageContext,
  resolveDeploymentProfile,
} from '../lib/deployment-profile';
import { ClosedStageParameters, closedDevParameter, devParameter } from '../parameters';

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

  test('closedDevParameter の既定は空の証明書・FQDN、空の許可 CIDR、検証用 EC2 なし、10.0.0.0/16', () => {
    expect(base).toEqual({
      certificateArn: '',
      domainName: '',
      allowedCidrs: [],
      createTestEnvironment: false,
      vpcCidr: '10.0.0.0/16',
    });
    expect(closedDevParameter.envName).toBe('dev-closed');
    expect(closedDevParameter.deploymentMode).toBe('closed');
    // 公開の dev の値（モデル・台数など）はそのまま写す
    expect(closedDevParameter.bedrockModelId).toBe(devParameter.bedrockModelId);
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
});
