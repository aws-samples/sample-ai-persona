import { Construct } from 'constructs';
import { Stack, StackProps, CfnOutput, RemovalPolicy, Duration } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';

export interface CognitoStackProps extends StackProps {
  envName: string;
  domainPrefix: string;
  cloudFrontDomainName: string;
  serviceEndpoint: string;
}

export class CognitoStack extends Stack {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;
  public readonly userPoolDomain: cognito.UserPoolDomain;

  constructor(scope: Construct, id: string, props: CognitoStackProps) {
    super(scope, id, props);

    const { envName, domainPrefix, cloudFrontDomainName, serviceEndpoint } = props;
    const isProd = envName === 'prod';

    // Callback URLs: CloudFront domain + Express Mode endpoint (for ALB Cognito auth)
    const callbackUrls = [
      `https://${cloudFrontDomainName}/oauth2/idpresponse`,
      `https://${serviceEndpoint}/oauth2/idpresponse`,
    ];
    const logoutUrls = [
      `https://${cloudFrontDomainName}/`,
      `https://${serviceEndpoint}/`,
    ];

    this.userPool = new cognito.UserPool(this, 'Default', {
      userPoolName: `ai-persona-${envName}`,
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      standardAttributes: { email: { required: true, mutable: true } },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: false,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: isProd ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    });

    this.userPoolDomain = this.userPool.addDomain('Domain', {
      cognitoDomain: { domainPrefix },
      managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
    });

    this.userPoolClient = this.userPool.addClient('AlbClient', {
      generateSecret: true,
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
        callbackUrls,
        logoutUrls,
      },
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      accessTokenValidity: Duration.hours(1),
      idTokenValidity: Duration.hours(1),
      refreshTokenValidity: Duration.days(30),
    });

    const managedLoginBranding = new cognito.CfnManagedLoginBranding(this, 'ManagedLoginBranding', {
      userPoolId: this.userPool.userPoolId,
      clientId: this.userPoolClient.userPoolClientId,
      useCognitoProvidedValues: true,
    });
    managedLoginBranding.node.addDependency(this.userPool);
    managedLoginBranding.node.addDependency(this.userPoolClient);

    // Outputs
    new CfnOutput(this, 'UserPoolId', {
      value: this.userPool.userPoolId,
      description: 'Cognito User Pool ID',
      exportName: `${id}-UserPoolId`,
    });
    new CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
      description: 'Cognito User Pool Client ID',
      exportName: `${id}-UserPoolClientId`,
    });
    new CfnOutput(this, 'UserPoolDomainName', {
      value: this.userPoolDomain.domainName,
      description: 'Cognito User Pool Domain Name',
      exportName: `${id}-UserPoolDomainName`,
    });
    new CfnOutput(this, 'CognitoDomainUrl', {
      value: `https://${this.userPoolDomain.domainName}.auth.${this.region}.amazoncognito.com`,
      description: 'Cognito Domain URL',
    });
    new CfnOutput(this, 'CallbackUrls', {
      value: callbackUrls.join(', '),
      description: 'OAuth Callback URLs (CloudFront + Express endpoint)',
    });
  }
}
