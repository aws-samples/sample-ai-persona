---
inclusion: manual
---

# ECS Express デプロイ手順

## 概要

ECS ExpressサービスにDockerイメージをデプロイする手順。
CloudFront + Lambda@Edge（cognito-at-edge）で認証しているため、ALBのCognito手動設定は不要。

## 前提条件
- AWS CLIが認証済みであること（`aws sts get-caller-identity` で確認）
- Dockerが起動していること

## 手順

### Step 1: 環境情報の取得

デプロイ対象のリソース情報を確認する。

```bash
# ECSクラスタ・サービスの確認
aws ecs list-clusters --region <REGION>
aws ecs list-services --cluster <CLUSTER_NAME> --region <REGION>

# CloudFrontドメインの確認
aws cloudformation describe-stacks \
  --stack-name AIPersona-dev --region <REGION> \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDomainName'].OutputValue" \
  --output text
```

### Step 2: Dockerイメージのビルド＆プッシュ

```bash
# ECRログイン
aws ecr get-login-password --region <REGION> | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com

# ビルド（ECS Fargate向けにamd64）
docker build --platform linux/amd64 -t <REPO_NAME>:latest .

# タグ付け＆プッシュ
docker tag <REPO_NAME>:latest <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/<REPO_NAME>:latest
docker push <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/<REPO_NAME>:latest
```

### Step 3: ECSサービスを更新

```bash
aws ecs update-service \
  --cluster <CLUSTER_NAME> \
  --service <SERVICE_NAME> \
  --force-new-deployment \
  --region <REGION>
```

### Step 4: デプロイ完了を待つ

```bash
aws ecs wait services-stable \
  --cluster <CLUSTER_NAME> \
  --services <SERVICE_NAME> \
  --region <REGION>
```

タイムアウトした場合は状態確認：
```bash
aws ecs describe-services \
  --cluster <CLUSTER_NAME> \
  --services <SERVICE_NAME> \
  --region <REGION> \
  --query 'services[0].{deployments:deployments[*].{status:status,running:runningCount,desired:desiredCount,rollout:rolloutState},events:events[0:3]}'
```

## 注意事項

- Lambda@Edge認証のため、デプロイ時にALBのCognito設定を外す必要はない
- CloudFrontの設定変更がある場合、反映に数分かかることがある
- Lambda@Edgeの更新はCDKデプロイ（`cdk deploy`）で行う

## トラブルシューティング

### デプロイがIN_PROGRESSのまま進まない
- ECSイベントを確認: ヘルスチェック失敗、ポート不一致、メモリ不足が多い
- タスクのログを確認: `aws ecs describe-tasks` でstoppedReasonを確認

### ロールバック
- ECRの前のイメージタグを指定して再デプロイ

### Lambda@Edge関連のエラー
- `redirect_mismatch`: CognitoのcallbackUrlにCloudFrontドメイン（末尾スラッシュあり/なし両方）が登録されているか確認
- `503 ERROR (Lambda function invalid)`: Lambda@Edgeのnode_modulesがデプロイされているか確認
- Lambda@Edge削除時にエラー: レプリカの削除に数時間かかる。時間を置いて再実行
