#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { EcrStack } from '../lib/ecr-stack';
import { CognitoStack } from '../lib/cognito-stack';
import { AIPersonaStack } from '../lib/ai-persona-stack';
import { AgentCoreMemoryStack } from '../lib/agentcore-memory-stack';
import { WafStack } from '../lib/waf-stack';
import { devParameter, prodParameter } from '../parameters';

const app = new cdk.App();

const envName = app.node.tryGetContext('env') || 'dev';
const parameter = envName === 'prod' ? prodParameter : devParameter;

// Step 0: AgentCore Memory Stack（オプション）
new AgentCoreMemoryStack(app, `AIPersonaMemory-${parameter.envName}`, {
  env: parameter.env,
  parameter,
  description: `AI Persona AgentCore Memory - ${parameter.envName}`,
});

// Step 1: ECR Stack
const ecrStack = new EcrStack(app, `AIPersonaEcr-${parameter.envName}`, {
  env: parameter.env,
  envName: parameter.envName,
  description: `AI Persona ECR Repository - ${parameter.envName}`,
});

// Step 2: Cognito Stack（先にデプロイ、IDをparameters.tsに設定）
new CognitoStack(app, `AIPersonaCognito-${parameter.envName}`, {
  env: parameter.env,
  envName: parameter.envName,
  domainPrefix: parameter.cognitoDomainPrefix,
  description: `AI Persona Cognito User Pool - ${parameter.envName}`,
});

// Step 3: WAF Stack（CloudFront用、us-east-1に作成必須）
let webAclArn: string | undefined;
if (parameter.enableWaf) {
  const wafStack = new WafStack(app, `AIPersonaWaf-${parameter.envName}`, {
    env: { account: parameter.env?.account, region: 'us-east-1' },
    envName: parameter.envName,
    crossRegionReferences: true,
    description: `AI Persona WAF WebACL - ${parameter.envName} (us-east-1)`,
  });
  webAclArn = wafStack.webAclArn;
}

// Step 4: Main Stack (ECS Express + CloudFront + Lambda@Edge Auth)
const mainStack = new AIPersonaStack(app, `AIPersona-${parameter.envName}`, {
  env: parameter.env,
  parameter,
  ecrRepository: ecrStack.repository,
  webAclArn,
  crossRegionReferences: true,
  description: `AI Persona System - ${parameter.envName} environment`,
});
mainStack.addDependency(ecrStack);
