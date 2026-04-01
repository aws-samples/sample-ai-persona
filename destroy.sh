#!/bin/bash
###############################################################################
# AI Persona System - 環境削除スクリプト（CloudShell版）
#
# deploy.sh でデプロイした全スタックを削除します。
# スタックの依存関係に基づき、正しい順序で削除を実行します。
#
# 使い方:
#   chmod +x destroy.sh && ./destroy.sh
#
# オプション:
#   --env ENV_NAME   環境名を指定（デフォルト: dev）
#   --region REGION  リージョンを指定（デフォルト: us-east-1）
#   --force          確認プロンプトをスキップ
###############################################################################
set -euo pipefail

# ===== 設定 =====
ENV_NAME="dev"
REGION="us-east-1"
FORCE=false

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
    --env)    ENV_NAME="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --force)  FORCE=true; shift ;;
    -h|--help)
      echo "使い方: ./destroy.sh [オプション]"
      echo "  --env NAME       環境名 (デフォルト: dev)"
      echo "  --region REGION  リージョン (デフォルト: us-east-1)"
      echo "  --force          確認プロンプトをスキップ"
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

# スタック名定義
COGNITO_STACK="AIPersonaCognito-${ENV_NAME}"
MAIN_STACK="AIPersona-${ENV_NAME}"
WAF_STACK="AIPersonaWaf-${ENV_NAME}"
MEMORY_STACK="AIPersonaMemory-${ENV_NAME}"
ECR_STACK="AIPersonaEcr-${ENV_NAME}"

# ===== 削除対象の確認 =====
log_step "削除対象スタックの確認"

stack_exists() {
  aws cloudformation describe-stacks --stack-name "$1" --region "${REGION}" > /dev/null 2>&1
}

STACKS_TO_DELETE=()
for stack in "${COGNITO_STACK}" "${MAIN_STACK}" "${MEMORY_STACK}" "${ECR_STACK}"; do
  if stack_exists "${stack}"; then
    log_info "  存在: ${stack}"
    STACKS_TO_DELETE+=("${stack}")
  else
    log_info "  なし: ${stack} (スキップ)"
  fi
done
# WAF Stack は us-east-1 に作成されるため別途確認
if aws cloudformation describe-stacks --stack-name "${WAF_STACK}" --region us-east-1 > /dev/null 2>&1; then
  log_info "  存在: ${WAF_STACK} (us-east-1)"
  STACKS_TO_DELETE+=("${WAF_STACK}")
else
  log_info "  なし: ${WAF_STACK} (スキップ)"
fi

if [[ ${#STACKS_TO_DELETE[@]} -eq 0 ]]; then
  log_info "削除対象のスタックがありません。"
  exit 0
fi

# ===== 確認プロンプト =====
if [[ "${FORCE}" == "false" ]]; then
  echo ""
  echo -e "${RED}╔══════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${RED}║  警告: 以下のスタックを完全に削除します                     ║${NC}"
  echo -e "${RED}╠══════════════════════════════════════════════════════════════╣${NC}"
  for stack in "${STACKS_TO_DELETE[@]}"; do
    echo -e "${RED}║${NC}  - ${stack}"
  done
  echo -e "${RED}╠══════════════════════════════════════════════════════════════╣${NC}"
  echo -e "${RED}║  DynamoDBテーブル、S3バケット内データも削除されます          ║${NC}"
  echo -e "${RED}╚══════════════════════════════════════════════════════════════╝${NC}"
  echo ""
  read -r -p "本当に削除しますか？ (yes/no): " CONFIRM
  if [[ "${CONFIRM}" != "yes" ]]; then
    log_info "キャンセルしました。"
    exit 0
  fi
fi

cd "${PROJECT_ROOT}/cdk"
npm install --silent 2>/dev/null

# ===== 削除実行（依存関係の逆順） =====

# 1. Cognito Stack
if stack_exists "${COGNITO_STACK}"; then
  log_step "Step 1: Cognito Stackの削除"
  npx cdk destroy "${COGNITO_STACK}" --force --region "${REGION}" 2>&1
  log_info "${COGNITO_STACK} を削除しました"
else
  log_info "Step 1: ${COGNITO_STACK} はスキップ"
fi

# 2. Main Stack
if stack_exists "${MAIN_STACK}"; then
  log_step "Step 2: メインスタックの削除"

  # prod環境はRemovalPolicy.RETAINのため、S3バケット・DynamoDBテーブルはスタック削除後も残ります
  # 必要に応じて手動で削除してください
  if [[ "${ENV_NAME}" == "prod" ]]; then
    log_warn "prod環境のため、S3バケットとDynamoDBテーブルはスタック削除後も残ります"
    log_warn "不要な場合はAWSコンソールまたはCLIで手動削除してください"
  fi

  npx cdk destroy "${MAIN_STACK}" --force --region "${REGION}" 2>&1 || {
    log_warn "${MAIN_STACK} の削除に失敗しました"
    log_warn "Lambda@Edgeのレプリカ削除に時間がかかっている可能性があります"
    log_warn "数時間後に再度 ./destroy.sh を実行してください"
  }
  log_info "${MAIN_STACK} を削除しました"
else
  log_info "Step 2: ${MAIN_STACK} はスキップ"
fi

# 3. WAF Stack (us-east-1)
if aws cloudformation describe-stacks --stack-name "${WAF_STACK}" --region us-east-1 > /dev/null 2>&1; then
  log_step "Step 3: WAF Stackの削除 (us-east-1)"
  npx cdk destroy "${WAF_STACK}" --force --region us-east-1 2>&1
  log_info "${WAF_STACK} を削除しました"
else
  log_info "Step 3: ${WAF_STACK} はスキップ"
fi

# 4. Memory Stack
if stack_exists "${MEMORY_STACK}"; then
  log_step "Step 4: AgentCore Memory Stackの削除"
  npx cdk destroy "${MEMORY_STACK}" --force --region "${REGION}" 2>&1
  log_info "${MEMORY_STACK} を削除しました"
else
  log_info "Step 4: ${MEMORY_STACK} はスキップ"
fi

# 5. ECR Stack
if stack_exists "${ECR_STACK}"; then
  log_step "Step 5: ECR Stackの削除"

  # ECRリポジトリ内のイメージを削除
  ECR_REPO=$(aws cloudformation describe-stack-resources \
    --stack-name "${ECR_STACK}" --region "${REGION}" \
    --query "StackResources[?ResourceType=='AWS::ECR::Repository'].PhysicalResourceId" \
    --output text 2>/dev/null || echo "")
  if [[ -n "${ECR_REPO}" && "${ECR_REPO}" != "None" ]]; then
    log_info "ECRイメージを削除しています: ${ECR_REPO}"
    IMAGE_IDS=$(aws ecr list-images --repository-name "${ECR_REPO}" --region "${REGION}" \
      --query 'imageIds[*]' --output json 2>/dev/null || echo "[]")
    if [[ "${IMAGE_IDS}" != "[]" ]]; then
      aws ecr batch-delete-image --repository-name "${ECR_REPO}" --region "${REGION}" \
        --image-ids "${IMAGE_IDS}" > /dev/null 2>&1 || true
    fi
  fi

  npx cdk destroy "${ECR_STACK}" --force --region "${REGION}" 2>&1
  log_info "${ECR_STACK} を削除しました"
else
  log_info "Step 5: ${ECR_STACK} はスキップ"
fi

# ===== 完了 =====
log_step "環境削除完了"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  AI Persona System 環境削除完了                             ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}  削除済みスタック:"
for stack in "${STACKS_TO_DELETE[@]}"; do
  echo -e "${GREEN}║${NC}    ✓ ${stack}"
done
if [[ "${ENV_NAME}" == "prod" ]]; then
  echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
  echo -e "${YELLOW}║  prod環境のため以下のリソースが残っています:               ${NC}"
  echo -e "${YELLOW}║    - S3バケット（データ含む）                              ${NC}"
  echo -e "${YELLOW}║    - DynamoDBテーブル（データ含む）                        ${NC}"
  echo -e "${YELLOW}║    - ECRリポジトリ（イメージ含む）                         ${NC}"
  echo -e "${YELLOW}║  不要な場合はAWSコンソールまたはCLIで手動削除してください  ${NC}"
fi
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
