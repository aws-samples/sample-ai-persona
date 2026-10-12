#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { AIPersonaStage } from '../lib/ai-persona-stage';
import { devParameter } from '../parameters';

/**
 * v1.0.0 の CDK アプリ: Stage AIPersonaDev（Data・App・Edge）だけを作る。
 *
 * v0 のスタック（AIPersona-dev、AIPersonaEcr-dev、AIPersonaCognito-dev など）は
 * このアプリから外した。デプロイ済みの v0 のスタックはそのまま残り、v1 と並べて動かせる
 * （切り戻しの備え。v0 の削除は利用者が明示的に判断して手で行う）。
 *
 * 使い方は deploy.sh を参照（① Data・Edge → ② イメージ → ③ App -c imageTag=<タグ>）。
 */
const app = new cdk.App();

new AIPersonaStage(app, 'AIPersonaDev', { parameter: devParameter });
