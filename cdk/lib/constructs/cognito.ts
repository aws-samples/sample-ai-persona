import { Construct } from 'constructs';
import { RemovalPolicy, Stack } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { acknowledgeAll, NAG_REASONS } from '../nag-reasons';

export interface CognitoProps {
  envName: string;
  /**
   * Whether to create the Managed Login domain (default true). The closed configuration
   * (ResolvedProfile.createCognitoDomain) sets false: no domain, and email recovery is disabled too.
   */
  createDomain?: boolean;
}

/**
 * User Pool password requirements (AwsSolutions-COG1).
 *
 * Symbols are required too. The five environment variables passed to the closed app
 * (PASSWORD_MIN_LENGTH, PASSWORD_REQUIRE_UPPERCASE, PASSWORD_REQUIRE_LOWERCASE, PASSWORD_REQUIRE_DIGITS,
 * PASSWORD_REQUIRE_SYMBOLS) are derived from this constant, so the values are not duplicated.
 * Existing passwords keep working; the rules apply from the next password change.
 */
export const PASSWORD_POLICY = {
  minLength: 8,
  requireLowercase: true,
  requireUppercase: true,
  requireDigits: true,
  requireSymbols: true,
} as const satisfies cognito.PasswordPolicy;

/**
 * The v1.0.0 Cognito User Pool (Data stack).
 *
 * - Essentials feature plan, no self sign-up (admins create users)
 * - Creates the role groups role:admin and role:member (department group:* are added operationally)
 * - Managed Login (new version) domain prefix ai-persona-v1-dev-{account ID}
 *   (not created in the closed configuration, where the app logs in via the admin API)
 * - RETAIN and deletion protection
 *
 * The app client and Managed Login branding live in the App stack because the callback URL
 * needs the CloudFront domain.
 */
export class Cognito extends Construct {
  public readonly userPool: cognito.UserPool;
  /** Managed Login domain (absent when createDomain is false). */
  public readonly domain?: cognito.UserPoolDomain;

  constructor(scope: Construct, id: string, props: CognitoProps) {
    super(scope, id);

    const { envName, createDomain = true } = props;
    const account = Stack.of(this).account;

    this.userPool = new cognito.UserPool(this, 'UserPool', {
      featurePlan: cognito.FeaturePlan.ESSENTIALS,
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      autoVerify: { email: true },
      standardAttributes: { email: { required: true, mutable: true } },
      passwordPolicy: { ...PASSWORD_POLICY },
      // Email does not arrive in the closed configuration, so recovery is by admin reset only
      accountRecovery: createDomain ? cognito.AccountRecovery.EMAIL_ONLY : cognito.AccountRecovery.NONE,
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

    if (createDomain) {
      this.domain = this.userPool.addDomain('Domain', {
        cognitoDomain: { domainPrefix: `ai-persona-v1-${envName}-${account}` },
        managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
      });
    }
  }

  /**
   * ``<prefix>.auth.<region>.amazoncognito.com`` (no scheme).
   *
   * Throws for a User Pool without a domain (closed configuration), so synth stops a wrong
   * public branch from passing an empty COGNITO_DOMAIN to the app.
   */
  public get domainHost(): string {
    if (!this.domain) {
      throw new Error('この User Pool にはドメインがない（閉域構成）');
    }
    return `${this.domain.domainName}.auth.${Stack.of(this).region}.amazoncognito.com`;
  }
}
