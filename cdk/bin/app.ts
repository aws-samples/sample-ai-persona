#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { EcrStack } from '../lib/ecr-stack';
import { CognitoStack } from '../lib/cognito-stack';
import { AIPersonaStack } from '../lib/ai-persona-stack';
import { AgentCoreMemoryStack } from '../lib/agentcore-memory-stack';
import { devParameter, prodParameter } from '../parameters';

const app = new cdk.App();

// 環境を取得（デフォルトはdev）
const envName = app.node.tryGetContext('env') || 'dev';
const parameter = envName === 'prod' ? prodParameter : devParameter;

// Step 0: AgentCore Memory Stack（オプション）
const memoryStack = new AgentCoreMemoryStack(app, `AIPersonaMemory-${parameter.envName}`, {
  env: parameter.env,
  parameter,
  description: `AI Persona AgentCore Memory - ${parameter.envName}`,
});

// Step 1: ECR Stack（最初にデプロイ）
const ecrStack = new EcrStack(app, `AIPersonaEcr-${parameter.envName}`, {
  env: parameter.env,
  envName: parameter.envName,
  description: `AI Persona ECR Repository - ${parameter.envName}`,
});

// Step 2: Main Stack（ECR Stackに依存）
// CloudFront + VPC Origin + WAF + Private Subnet構成
const mainStack = new AIPersonaStack(app, `AIPersona-${parameter.envName}`, {
  env: parameter.env,
  parameter,
  ecrRepository: ecrStack.repository,
  description: `AI Persona System - ${parameter.envName} environment`,
});
mainStack.addDependency(ecrStack);

// Step 3: Cognito Stack（Main Stackに依存、CloudFrontドメインを使用）
const cognitoStack = new CognitoStack(app, `AIPersonaCognito-${parameter.envName}`, {
  env: parameter.env,
  envName: parameter.envName,
  domainPrefix: parameter.cognitoDomainPrefix,
  cloudFrontDomainName: mainStack.cloudFrontDomainName,
  description: `AI Persona Cognito User Pool - ${parameter.envName}`,
});
cognitoStack.addDependency(mainStack);
