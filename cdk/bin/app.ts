#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { applyNagChecks, createDevStage } from '../lib/dev-stage';

/**
 * v1.0.0 の CDK アプリ: Stage AIPersonaDev（Data・App・Edge）だけを作る。
 *
 * v0 のスタック（AIPersona-dev、AIPersonaEcr-dev、AIPersonaCognito-dev など）は
 * このアプリから外した。デプロイ済みの v0 のスタックはそのまま残り、v1 と並べて動かせる
 * （切り戻しの備え。v0 の削除は利用者が明示的に判断して手で行う）。
 *
 * Stage の組み立てと cdk-nag の掛け方は lib/dev-stage.ts（テストも同じ関数を通る。U9）。
 * 使い方は deploy.sh を参照（① Data・Edge → ② イメージ → ③ App -c imageTag=<タグ>）。
 */
const app = new cdk.App();

createDevStage(app);
applyNagChecks(app);
