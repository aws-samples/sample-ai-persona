#!/bin/bash
###############################################################################
# AI Persona System - ワンクリックデプロイスクリプト（CloudShell版）
#
# AWS CloudShell環境で実行してください。
# このスクリプトだけで、CDKの知識がなくてもAWSにデプロイできます。
#
# 使い方:
#   1. CloudShellでリポジトリをクローン
#      git clone https://github.com/aws-samples/sample-ai-persona.git
#   2. cd sample-ai-persona
#   3. chmod +x deploy.sh && ./deploy.sh
#
# オプション:
#   --skip-memory    長期記憶機能（AgentCore Memory）をスキップ
#   --skip-cognito   Cognito認証をスキップ
#   --env ENV_NAME   環境名を指定（デフォルト: dev）
#   --region REGION  リージョンを指定（デフォルト: us-east-1）
###############################################################################
set -euo pipefail

# ===== 設定 =====
ENV_NAME="dev"
REGION="us-east-1"
SKIP_MEMORY=false
SKIP_COGNITO=false

# カラー出力
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "\n${BLUE}========================================${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}========================================${NC}"; }

# ===== 引数パース =====
while [[ $# -gt 0 ]]; do
  case $1 in
    --skip-memory)  SKIP_MEMORY=true; shift ;;
    --skip-cognito) SKIP_COGNITO=true; shift ;;
    --env)          ENV_NAME="$2"; shift 2 ;;
    --region)       REGION="$2"; shift 2 ;;
    -h|--help)
      echo "使い方: ./deploy.sh [オプション]"
      echo "  --skip-memory    長期記憶機能をスキップ"
      echo "  --skip-cognito   Cognito認証をスキップ"
      echo "  --env NAME       環境名 (デフォルト: dev)"
      echo "  --region REGION  リージョン (デフォルト: us-east-1)"
      exit 0 ;;
    *) log_error "不明なオプション: $1"; exit 1 ;;
  esac
done

# ===== 環境チェック =====
log_step "Step 0: 環境チェック"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
log_info "AWSアカウント: ${ACCOUNT_ID}"
log_info "リージョン: ${REGION}"
log_info "環境名: ${ENV_NAME}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "${SCRIPT_DIR}/cdk/bin/app.ts" ]]; then
  log_error "CDKプロジェクトが見つかりません。プロジェクトルートで実行してください。"
  exit 1
fi
PROJECT_ROOT="${SCRIPT_DIR}"

# Docker確認
if ! docker info > /dev/null 2>&1; then
  log_error "Dockerが利用できません。CloudShell環境で実行してください。"
  exit 1
fi
log_info "Docker: OK"

# ===== CDK依存関係インストール =====
log_step "Step 1: CDK依存関係のインストール"

cd "${PROJECT_ROOT}/cdk"
npm install
log_info "npm install 完了"

# ===== parameters.ts の動的生成 =====
log_step "Step 2: デプロイパラメータの設定"

RANDOM_SUFFIX=$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 8)
COGNITO_DOMAIN_PREFIX="ai-persona-${ENV_NAME}-${RANDOM_SUFFIX}"

if [[ "${ENV_NAME}" == "prod" ]]; then
  TABLE_PREFIX="AIPersona"
  CONTAINER_CPU="2048"
  CONTAINER_MEMORY="8192"
  ENABLE_WAF="true"
else
  TABLE_PREFIX="AIPersonaDev"
  CONTAINER_CPU="1024"
  CONTAINER_MEMORY="4096"
  ENABLE_WAF="false"
fi

cat > "${PROJECT_ROOT}/cdk/parameters.ts" << PARAMS_EOF
import { Environment } from 'aws-cdk-lib';

export interface AppParameter {
  env?: Environment;
  envName: string;
  dynamoDbTablePrefix: string;
  cognitoDomainPrefix: string;
  containerCpu: string;
  containerMemory: string;
  enableWaf?: boolean;
  cognitoUserPoolId: string;
  cognitoUserPoolAppId: string;
  cognitoUserPoolDomain: string;
  agentCoreMemoryId: string;
  summaryMemoryStrategyId: string;
  semanticMemoryStrategyId: string;
  agentCoreMemoryEventExpiryDays?: number;
  bedrockModelId: string;
  agentModelId: string;
  batchInferenceModelId: string;
  surveyS3Prefix?: string;
  batchInferenceS3Prefix?: string;
}

export const devParameter: AppParameter = {
  env: {
    account: '${ACCOUNT_ID}',
    region: '${REGION}',
  },
  envName: '${ENV_NAME}',
  dynamoDbTablePrefix: '${TABLE_PREFIX}',
  cognitoDomainPrefix: '${COGNITO_DOMAIN_PREFIX}',
  containerCpu: '${CONTAINER_CPU}',
  containerMemory: '${CONTAINER_MEMORY}',
  enableWaf: ${ENABLE_WAF},
  cognitoUserPoolId: '',
  cognitoUserPoolAppId: '',
  cognitoUserPoolDomain: '',
  agentCoreMemoryId: '',
  summaryMemoryStrategyId: '',
  semanticMemoryStrategyId: '',
  agentCoreMemoryEventExpiryDays: 30,
  bedrockModelId: 'global.anthropic.claude-sonnet-4-5-20250929-v1:0',
  agentModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  batchInferenceModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  surveyS3Prefix: 'survey-results/',
  batchInferenceS3Prefix: 'batch-inference/',
};

export const prodParameter: AppParameter = devParameter;
PARAMS_EOF

log_info "parameters.ts を生成しました"

# ===== CDK Bootstrap =====
log_step "Step 3: CDK Bootstrap"

cd "${PROJECT_ROOT}/cdk"
npx cdk bootstrap "aws://${ACCOUNT_ID}/${REGION}" --region "${REGION}" 2>&1 || {
  log_warn "Bootstrap済みの可能性があります。続行します。"
}

# ===== ECR Stack デプロイ =====
log_step "Step 4: ECRリポジトリの作成"

npx cdk deploy "AIPersonaEcr-${ENV_NAME}" --require-approval never --region "${REGION}" 2>&1
ECR_REPO_URI=$(aws cloudformation describe-stacks \
  --stack-name "AIPersonaEcr-${ENV_NAME}" \
  --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='RepositoryUri'].OutputValue" \
  --output text)
log_info "ECRリポジトリ: ${ECR_REPO_URI}"

# ===== Dockerビルド・プッシュ（CloudShell内） =====
log_step "Step 5: Dockerイメージのビルド・プッシュ"

cd "${PROJECT_ROOT}"

aws ecr get-login-password --region "${REGION}" | \
  docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

IMAGE_TAG=$(date +%Y%m%d%H%M%S)
log_info "Dockerイメージをビルド中... (tag: ${IMAGE_TAG})"
docker build -t ai-persona .

docker tag ai-persona:latest "${ECR_REPO_URI}:${IMAGE_TAG}"
docker tag ai-persona:latest "${ECR_REPO_URI}:latest"

log_info "ECRにプッシュ中..."
docker push "${ECR_REPO_URI}:${IMAGE_TAG}"
docker push "${ECR_REPO_URI}:latest"
log_info "Dockerイメージのプッシュ完了 (tag: ${IMAGE_TAG})"

# ===== AgentCore Memory（オプション）=====
if [[ "${SKIP_MEMORY}" == "false" ]]; then
  log_step "Step 6: AgentCore Memory（長期記憶）のデプロイ"

  cd "${PROJECT_ROOT}/cdk"
  npx cdk deploy "AIPersonaMemory-${ENV_NAME}" --require-approval never --region "${REGION}" 2>&1 || {
    log_warn "AgentCore Memoryのデプロイに失敗しました。長期記憶なしで続行します。"
    SKIP_MEMORY=true
  }

  if [[ "${SKIP_MEMORY}" == "false" ]]; then
    MEMORY_ID=$(aws cloudformation describe-stacks \
      --stack-name "AIPersonaMemory-${ENV_NAME}" \
      --region "${REGION}" \
      --query "Stacks[0].Outputs[?OutputKey=='MemoryId'].OutputValue" \
      --output text)
    log_info "Memory ID: ${MEMORY_ID}"

    log_info "Strategy IDを取得中..."
    SUMMARY_STRATEGY_ID=""
    SEMANTIC_STRATEGY_ID=""
    for i in $(seq 1 12); do
      SUMMARY_STRATEGY_ID=$(aws bedrock-agentcore-control get-memory \
        --memory-id "${MEMORY_ID}" --region "${REGION}" \
        --query 'memory.strategies[?type==`SUMMARIZATION`].strategyId' \
        --output text 2>/dev/null || echo "")
      SEMANTIC_STRATEGY_ID=$(aws bedrock-agentcore-control get-memory \
        --memory-id "${MEMORY_ID}" --region "${REGION}" \
        --query 'memory.strategies[?type==`SEMANTIC`].strategyId' \
        --output text 2>/dev/null || echo "")
      if [[ -n "${SUMMARY_STRATEGY_ID}" && "${SUMMARY_STRATEGY_ID}" != "None" && \
            -n "${SEMANTIC_STRATEGY_ID}" && "${SEMANTIC_STRATEGY_ID}" != "None" ]]; then
        break
      fi
      log_info "  待機中... (${i}/12)"
      sleep 10
    done
    log_info "Summary Strategy ID: ${SUMMARY_STRATEGY_ID}"
    log_info "Semantic Strategy ID: ${SEMANTIC_STRATEGY_ID}"

    sed -i "s/agentCoreMemoryId: ''/agentCoreMemoryId: '${MEMORY_ID}'/" "${PROJECT_ROOT}/cdk/parameters.ts"
    sed -i "s/summaryMemoryStrategyId: ''/summaryMemoryStrategyId: '${SUMMARY_STRATEGY_ID}'/" "${PROJECT_ROOT}/cdk/parameters.ts"
    sed -i "s/semanticMemoryStrategyId: ''/semanticMemoryStrategyId: '${SEMANTIC_STRATEGY_ID}'/" "${PROJECT_ROOT}/cdk/parameters.ts"
    log_info "parameters.ts を更新しました"
  fi
else
  log_info "長期記憶機能をスキップします"

  # 既存のMemoryスタックからIDを取得してparameters.tsに反映（再デプロイ時のリセット防止）
  if aws cloudformation describe-stacks --stack-name "AIPersonaMemory-${ENV_NAME}" --region "${REGION}" > /dev/null 2>&1; then
    log_info "既存のMemoryスタックからIDを取得中..."
    MEMORY_ID=$(aws cloudformation describe-stacks \
      --stack-name "AIPersonaMemory-${ENV_NAME}" \
      --region "${REGION}" \
      --query "Stacks[0].Outputs[?OutputKey=='MemoryId'].OutputValue" \
      --output text)
    if [[ -n "${MEMORY_ID}" && "${MEMORY_ID}" != "None" ]]; then
      SUMMARY_STRATEGY_ID=$(aws bedrock-agentcore-control get-memory \
        --memory-id "${MEMORY_ID}" --region "${REGION}" \
        --query 'memory.strategies[?type==`SUMMARIZATION`].strategyId' \
        --output text 2>/dev/null || echo "")
      SEMANTIC_STRATEGY_ID=$(aws bedrock-agentcore-control get-memory \
        --memory-id "${MEMORY_ID}" --region "${REGION}" \
        --query 'memory.strategies[?type==`SEMANTIC`].strategyId' \
        --output text 2>/dev/null || echo "")

      sed -i "s/agentCoreMemoryId: ''/agentCoreMemoryId: '${MEMORY_ID}'/" "${PROJECT_ROOT}/cdk/parameters.ts"
      sed -i "s/summaryMemoryStrategyId: ''/summaryMemoryStrategyId: '${SUMMARY_STRATEGY_ID}'/" "${PROJECT_ROOT}/cdk/parameters.ts"
      sed -i "s/semanticMemoryStrategyId: ''/semanticMemoryStrategyId: '${SEMANTIC_STRATEGY_ID}'/" "${PROJECT_ROOT}/cdk/parameters.ts"
      log_info "既存のMemory設定をparameters.tsに反映しました"
    fi
  fi
fi

# ===== Cognito デプロイ（メインスタックより先） =====
if [[ "${SKIP_COGNITO}" == "false" ]]; then
  log_step "Step 7: Cognito認証のデプロイ"

  cd "${PROJECT_ROOT}/cdk"
  npx cdk deploy "AIPersonaCognito-${ENV_NAME}" --require-approval never --region "${REGION}" 2>&1

  COGNITO_USER_POOL_ID=$(aws cloudformation describe-stacks \
    --stack-name "AIPersonaCognito-${ENV_NAME}" \
    --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
    --output text)
  COGNITO_CLIENT_ID=$(aws cloudformation describe-stacks \
    --stack-name "AIPersonaCognito-${ENV_NAME}" \
    --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" \
    --output text)
  COGNITO_DOMAIN=$(aws cloudformation describe-stacks \
    --stack-name "AIPersonaCognito-${ENV_NAME}" \
    --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolDomainName'].OutputValue" \
    --output text)

  log_info "Cognito User Pool ID: ${COGNITO_USER_POOL_ID}"
  log_info "Cognito Client ID: ${COGNITO_CLIENT_ID}"
  log_info "Cognito Domain: ${COGNITO_DOMAIN}"

  sed -i "s/cognitoUserPoolId: ''/cognitoUserPoolId: '${COGNITO_USER_POOL_ID}'/" "${PROJECT_ROOT}/cdk/parameters.ts"
  sed -i "s/cognitoUserPoolAppId: ''/cognitoUserPoolAppId: '${COGNITO_CLIENT_ID}'/" "${PROJECT_ROOT}/cdk/parameters.ts"
  sed -i "s/cognitoUserPoolDomain: ''/cognitoUserPoolDomain: '${COGNITO_DOMAIN}'/" "${PROJECT_ROOT}/cdk/parameters.ts"
  log_info "parameters.ts にCognito設定を反映しました"
else
  log_info "Cognito認証をスキップします"

  # 既存のCognitoスタックからIDを取得してparameters.tsに反映（再デプロイ時のリセット防止）
  if aws cloudformation describe-stacks --stack-name "AIPersonaCognito-${ENV_NAME}" --region "${REGION}" > /dev/null 2>&1; then
    log_info "既存のCognitoスタックからIDを取得中..."
    COGNITO_USER_POOL_ID=$(aws cloudformation describe-stacks \
      --stack-name "AIPersonaCognito-${ENV_NAME}" \
      --region "${REGION}" \
      --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
      --output text)
    COGNITO_CLIENT_ID=$(aws cloudformation describe-stacks \
      --stack-name "AIPersonaCognito-${ENV_NAME}" \
      --region "${REGION}" \
      --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" \
      --output text)
    COGNITO_DOMAIN=$(aws cloudformation describe-stacks \
      --stack-name "AIPersonaCognito-${ENV_NAME}" \
      --region "${REGION}" \
      --query "Stacks[0].Outputs[?OutputKey=='UserPoolDomainName'].OutputValue" \
      --output text)

    sed -i "s/cognitoUserPoolId: ''/cognitoUserPoolId: '${COGNITO_USER_POOL_ID}'/" "${PROJECT_ROOT}/cdk/parameters.ts"
    sed -i "s/cognitoUserPoolAppId: ''/cognitoUserPoolAppId: '${COGNITO_CLIENT_ID}'/" "${PROJECT_ROOT}/cdk/parameters.ts"
    sed -i "s/cognitoUserPoolDomain: ''/cognitoUserPoolDomain: '${COGNITO_DOMAIN}'/" "${PROJECT_ROOT}/cdk/parameters.ts"
    log_info "既存のCognito設定をparameters.tsに反映しました"
  fi
fi

# ===== メインスタック デプロイ =====
log_step "Step 8: メインスタック（ECS + CloudFront + Lambda@Edge認証）のデプロイ"

cd "${PROJECT_ROOT}/cdk"

# Lambda@Edge依存関係インストール
if [[ -f "${PROJECT_ROOT}/cdk/lambda/auth-at-edge/package.json" ]]; then
  cd "${PROJECT_ROOT}/cdk/lambda/auth-at-edge"
  npm install --silent 2>/dev/null
  cd "${PROJECT_ROOT}/cdk"
fi

npx cdk deploy "AIPersona-${ENV_NAME}" --require-approval never --region "${REGION}" -c "imageTag=${IMAGE_TAG}" 2>&1

CLOUDFRONT_DOMAIN=$(aws cloudformation describe-stacks \
  --stack-name "AIPersona-${ENV_NAME}" \
  --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDomainName'].OutputValue" \
  --output text)
log_info "CloudFrontドメイン: https://${CLOUDFRONT_DOMAIN}"

# ===== Cognito callbackUrl更新（CloudFrontドメイン確定後） =====
if [[ "${SKIP_COGNITO}" == "false" && -n "${COGNITO_USER_POOL_ID}" && -n "${COGNITO_CLIENT_ID}" ]]; then
  log_step "Step 9: Cognito callbackUrl更新"

  aws cognito-idp update-user-pool-client \
    --user-pool-id "${COGNITO_USER_POOL_ID}" \
    --client-id "${COGNITO_CLIENT_ID}" \
    --region "${REGION}" \
    --supported-identity-providers COGNITO \
    --callback-urls "https://${CLOUDFRONT_DOMAIN}" "https://${CLOUDFRONT_DOMAIN}/" \
    --logout-urls "https://${CLOUDFRONT_DOMAIN}" "https://${CLOUDFRONT_DOMAIN}/" \
    --allowed-o-auth-flows code \
    --allowed-o-auth-scopes openid email profile \
    --allowed-o-auth-flows-user-pool-client 2>&1 || {
    log_warn "callbackUrl自動更新に失敗しました。Cognitoコンソールで手動設定してください"
    log_warn "  Callback URL: https://${CLOUDFRONT_DOMAIN}/"
  }
  log_info "Cognito callbackUrlを https://${CLOUDFRONT_DOMAIN}/ に更新しました"
fi

# ===== 完了 =====
log_step "デプロイ完了！"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  AI Persona System デプロイ完了                             ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}  アプリURL: ${BLUE}https://${CLOUDFRONT_DOMAIN}${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}  再デプロイ（コード更新時）:"
echo -e "${GREEN}║${NC}    ./deploy.sh --skip-memory --skip-cognito --region ${REGION}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
