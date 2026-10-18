import { Construct } from 'constructs';
import { RemovalPolicy, Stack } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { acknowledgeAll, NAG_REASONS } from '../nag-reasons';

export interface CognitoProps {
  envName: string;
}

/**
 * User Pool のパスワードの条件（U9、Infrastructure Design Q2 A、AwsSolutions-COG1）。
 *
 * 記号も求める。閉域構成のアプリに渡す 5 つの環境変数（PASSWORD_MIN_LENGTH・
 * PASSWORD_REQUIRE_UPPERCASE・PASSWORD_REQUIRE_LOWERCASE・PASSWORD_REQUIRE_DIGITS・
 * PASSWORD_REQUIRE_SYMBOLS）も、閉域スタックのリリースでこの定数から作る（値を二重に持たない）。
 * 今のパスワードはそのまま使え、次に決めるときから効く。
 */
export const PASSWORD_POLICY = {
  minLength: 8,
  requireLowercase: true,
  requireUppercase: true,
  requireDigits: true,
  requireSymbols: true,
} as const satisfies cognito.PasswordPolicy;

/**
 * v1.0.0 の Cognito User Pool（Data スタック）。
 *
 * - 機能プランは Essentials、セルフサインアップなし（管理者が利用者を作る）
 * - ロールのグループ role:admin・role:member を作る（部署の group:* は運用で足す。U2）
 * - Managed Login（新しい版）のドメイン接頭辞 ai-persona-v1-dev-{アカウント ID}
 * - RETAIN と削除保護
 *
 * アプリクライアントと Managed Login のブランディングは、コールバック URL に
 * CloudFront のドメインが要るため App スタックに置く（logical-components.md）。
 */
export class Cognito extends Construct {
  public readonly userPool: cognito.UserPool;
  public readonly domain: cognito.UserPoolDomain;

  constructor(scope: Construct, id: string, props: CognitoProps) {
    super(scope, id);

    const { envName } = props;
    const account = Stack.of(this).account;

    this.userPool = new cognito.UserPool(this, 'UserPool', {
      featurePlan: cognito.FeaturePlan.ESSENTIALS,
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      autoVerify: { email: true },
      standardAttributes: { email: { required: true, mutable: true } },
      passwordPolicy: { ...PASSWORD_POLICY },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      deletionProtection: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    acknowledgeAll(this.userPool, ['COG2'], NAG_REASONS.cognitoMfa);
    acknowledgeAll(this.userPool, ['COG8'], NAG_REASONS.cognitoPlusPlan);

    for (const role of ['admin', 'member']) {
      new cognito.UserPoolGroup(this, `RoleGroup-${role}`, {
        userPool: this.userPool,
        groupName: `role:${role}`,
        description: `Application role ${role}`,
      });
    }

    this.domain = this.userPool.addDomain('Domain', {
      cognitoDomain: { domainPrefix: `ai-persona-v1-${envName}-${account}` },
      managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
    });
  }

  /** ``<prefix>.auth.<region>.amazoncognito.com``（スキームなし）。 */
  public get domainHost(): string {
    return `${this.domain.domainName}.auth.${Stack.of(this).region}.amazoncognito.com`;
  }
}
