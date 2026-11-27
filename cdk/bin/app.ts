#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { applyNagChecks, createClosedDevStage, createDevStage } from '../lib/dev-stage';

/**
 * CDK app for v1.0.0: creates the AIPersonaDev Stage (Data, App, Edge). Only when the closed-network
 * parameters (-c closedCertificateArn=... -c closedDomainName=...) are passed, it also creates the
 * AIPersonaDevClosed Stage (Data, App) alongside it (deploy.sh --closed passes them).
 *
 * The v0 stacks (AIPersona-dev, AIPersonaEcr-dev, AIPersonaCognito-dev, etc.) are no longer part of
 * this app. Deployed v0 stacks stay as they are and can run next to v1 as a rollback option;
 * deleting v0 is a manual decision by the user.
 *
 * Stage assembly and cdk-nag wiring live in lib/dev-stage.ts (tests go through the same functions).
 * See deploy.sh for usage (1. Data/Edge, 2. image, 3. App with -c imageTag=<tag>).
 */
const app = new cdk.App();

createDevStage(app);
createClosedDevStage(app);
applyNagChecks(app);
