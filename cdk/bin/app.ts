#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { EcrStack } from '../lib/ecr-stack';
import { CognitoStack } from '../lib/cognito-stack';
import { AIPersonaStack } from '../lib/ai-persona-stack';
import { AgentCoreMemoryStack } from '../lib/agentcore-memory-stack';
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
const cognitoStack = new CognitoStack(app, `AIPersonaCognito-${parameter.envName}`, {
  env: parameter.env,
  envName: parameter.envName,
  domainPrefix: parameter.cognitoDomainPrefix,
  description: `AI Persona Cognito User Pool - ${parameter.envName}`,
});

// Step 3: Main Stack (ECS Express + CloudFront + WAF + Lambda@Edge Auth)
// 注意: CognitoStackをデプロイ後、parameters.tsにCognito IDを設定してからデプロイ
const mainStack = new AIPersonaStack(app, `AIPersona-${parameter.envName}`, {
  env: parameter.env,
  parameter,
  ecrRepository: ecrStack.repository,
  description: `AI Persona System - ${parameter.envName} environment`,
});
mainStack.addDependency(ecrStack);
