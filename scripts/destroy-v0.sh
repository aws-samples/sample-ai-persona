#!/bin/bash
###############################################################################
# AI Persona System - v0 environment teardown script (CloudShell)
#
# Removes every stack deployed by the v0 deploy.sh (before v1.0.0),
# in the order required by the stack dependencies.
# Stacks of v1.0.0 (AIPersonaDev / AIPersonaDevClosed) are removed with ./destroy.sh in the
# project root. This script does not touch v1 stacks.
#
# v0 can be kept as a rollback option for v1. Remove it yourself only after
# confirming v1 works.
#
# Usage (run from the project root):
#   ./scripts/destroy-v0.sh
#
# Options:
#   --env ENV_NAME   Environment name (default: dev)
#   --region REGION  Region (default: us-east-1)
#   --force          Skip the confirmation prompt
###############################################################################
set -euo pipefail

# ===== Settings =====
ENV_NAME="dev"
REGION="us-east-1"
FORCE=false

# Colored output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "\n${BLUE}========================================${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}========================================${NC}"; }

# ===== Argument parsing =====
while [[ $# -gt 0 ]]; do
  case $1 in
    --env)    ENV_NAME="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --force)  FORCE=true; shift ;;
    -h|--help)
      echo "使い方: ./scripts/destroy-v0.sh [オプション]"
      echo "  v0 のスタック（AIPersona-<env> など）を削除します。v1 は ./destroy.sh を使います。"
      echo "  --env NAME       環境名 (デフォルト: dev)"
      echo "  --region REGION  リージョン (デフォルト: us-east-1)"
      echo "  --force          確認プロンプトをスキップ"
      exit 0 ;;
    *) log_error "不明なオプション: $1"; exit 1 ;;
  esac
done

# ===== Environment checks =====
log_step "Step 0: 環境チェック"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
log_info "AWSアカウント: ${ACCOUNT_ID}"
log_info "リージョン: ${REGION}"
log_info "環境名: ${ENV_NAME}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ ! -f "${PROJECT_ROOT}/cdk/bin/app.ts" ]]; then
  log_error "CDKプロジェクトが見つかりません。scripts/ に置いたまま実行してください。"
  exit 1
fi

# Stack names
MCP_STACK="AIPersonaMcp-${ENV_NAME}"
COGNITO_STACK="AIPersonaCognito-${ENV_NAME}"
MAIN_STACK="AIPersona-${ENV_NAME}"
WAF_STACK="AIPersonaWaf-${ENV_NAME}"
MEMORY_STACK="AIPersonaMemory-${ENV_NAME}"
ECR_STACK="AIPersonaEcr-${ENV_NAME}"
# Lambda@Edge support stack (auto-created in us-east-1 when REGION is not us-east-1).
# Its name is dynamic (edge-lambda-stack-<hash>), so it is identified at detection time.
EDGE_STACK=""

# ===== Confirm what will be removed =====
log_step "削除対象スタックの確認"

stack_exists() {
  aws cloudformation describe-stacks --stack-name "$1" --region "${REGION}" > /dev/null 2>&1
}

# Find the Lambda@Edge support stack of this project (this env name) among edge-lambda-stack-* in us-east-1,
# by looking for the function name ai-persona-auth-edge-${ENV_NAME}.
find_edge_stack() {
  local candidates s
  candidates=$(aws cloudformation list-stacks --region us-east-1 \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE IMPORT_COMPLETE DELETE_FAILED \
    --query "StackSummaries[?starts_with(StackName, 'edge-lambda-stack')].StackName" \
    --output text 2>/dev/null || echo "")
  for s in ${candidates}; do
    if aws cloudformation describe-stack-resources --stack-name "${s}" --region us-east-1 \
        --query "StackResources[?ResourceType=='AWS::Lambda::Function'].PhysicalResourceId" \
        --output text 2>/dev/null | grep -q "ai-persona-auth-edge-${ENV_NAME}"; then
      echo "${s}"
      return 0
    fi
  done
  echo ""
}

STACKS_TO_DELETE=()
for stack in "${MCP_STACK}" "${COGNITO_STACK}" "${MAIN_STACK}" "${MEMORY_STACK}" "${ECR_STACK}"; do
  if stack_exists "${stack}"; then
    log_info "  存在: ${stack}"
    STACKS_TO_DELETE+=("${stack}")
  else
    log_info "  なし: ${stack} (スキップ)"
  fi
done
# The WAF stack is created in us-east-1, so check it separately
if aws cloudformation describe-stacks --stack-name "${WAF_STACK}" --region us-east-1 > /dev/null 2>&1; then
  log_info "  存在: ${WAF_STACK} (us-east-1)"
  STACKS_TO_DELETE+=("${WAF_STACK}")
else
  log_info "  なし: ${WAF_STACK} (スキップ)"
fi
# Lambda@Edge support stack (searched in us-east-1 only when REGION is not us-east-1)
if [[ "${REGION}" != "us-east-1" ]]; then
  EDGE_STACK=$(find_edge_stack)
  if [[ -n "${EDGE_STACK}" ]]; then
    log_info "  存在: ${EDGE_STACK} (us-east-1 / Lambda@Edge)"
    STACKS_TO_DELETE+=("${EDGE_STACK}")
  else
    log_info "  なし: Lambda@Edge サポートスタック (us-east-1) (スキップ)"
  fi
fi

if [[ ${#STACKS_TO_DELETE[@]} -eq 0 ]]; then
  log_info "削除対象のスタックがありません。"
  exit 0
fi

# ===== Confirmation prompt =====
if [[ "${FORCE}" == "false" ]]; then
  echo ""
  echo -e "${RED}╔══════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${RED}║  警告: 以下の v0 スタックを完全に削除します                 ║${NC}"
  echo -e "${RED}╠══════════════════════════════════════════════════════════════╣${NC}"
  for stack in "${STACKS_TO_DELETE[@]}"; do
    echo -e "${RED}║${NC}  - ${stack}"
  done
  echo -e "${RED}╠══════════════════════════════════════════════════════════════╣${NC}"
  echo -e "${RED}║  DynamoDBテーブル、S3バケット内データも削除されます          ║${NC}"
  echo -e "${RED}║  （prod 環境ではスタック削除後も残ります）                   ║${NC}"
  echo -e "${RED}║  v0 を消すと v1 から v0 へ切り戻せなくなります               ║${NC}"
  echo -e "${RED}╚══════════════════════════════════════════════════════════════╝${NC}"
  echo ""
  read -r -p "本当に削除しますか？ (yes/no): " CONFIRM
  if [[ "${CONFIRM}" != "yes" ]]; then
    log_info "キャンセルしました。"
    exit 0
  fi
fi

delete_stack() {
  local stack_name="$1"
  local region="$2"
  aws cloudformation delete-stack --stack-name "${stack_name}" --region "${region}"
  log_info "${stack_name} の削除を開始しました。完了を待機中..."
  aws cloudformation wait stack-delete-complete --stack-name "${stack_name}" --region "${region}" 2>&1 || {
    log_warn "${stack_name} の削除に失敗またはタイムアウトしました"
    log_warn "Lambda@Edgeのレプリカ削除に時間がかかっている可能性があります"
    log_warn "数時間後に再度 ./scripts/destroy-v0.sh を実行してください"
    return 1
  }
  log_info "${stack_name} を削除しました"
}

# ===== Removal (reverse dependency order) =====

# 1. MCP Gateway stack (first, because it imports exports of the main stack)
if stack_exists "${MCP_STACK}"; then
  log_step "Step 1: MCP Gateway Stackの削除"
  delete_stack "${MCP_STACK}" "${REGION}"
else
  log_info "Step 1: ${MCP_STACK} はスキップ"
fi

# 2. Cognito Stack
if stack_exists "${COGNITO_STACK}"; then
  log_step "Step 2: Cognito Stackの削除"
  delete_stack "${COGNITO_STACK}" "${REGION}"
else
  log_info "Step 2: ${COGNITO_STACK} はスキップ"
fi

# 3. Main Stack
if stack_exists "${MAIN_STACK}"; then
  log_step "Step 3: メインスタックの削除"

  if [[ "${ENV_NAME}" == "prod" ]]; then
    log_warn "prod環境のため、S3バケットとDynamoDBテーブルはスタック削除後も残ります"
    log_warn "不要な場合はAWSコンソールまたはCLIで手動削除してください"
  fi

  delete_stack "${MAIN_STACK}" "${REGION}"
else
  log_info "Step 3: ${MAIN_STACK} はスキップ"
fi

# 4. WAF Stack (us-east-1)
if aws cloudformation describe-stacks --stack-name "${WAF_STACK}" --region us-east-1 > /dev/null 2>&1; then
  log_step "Step 4: WAF Stackの削除 (us-east-1)"
  delete_stack "${WAF_STACK}" "us-east-1"
else
  log_info "Step 4: ${WAF_STACK} はスキップ"
fi

# 5. Memory Stack
if stack_exists "${MEMORY_STACK}"; then
  log_step "Step 5: AgentCore Memory Stackの削除"
  delete_stack "${MEMORY_STACK}" "${REGION}"
else
  log_info "Step 5: ${MEMORY_STACK} はスキップ"
fi

# 6. ECR Stack
if stack_exists "${ECR_STACK}"; then
  log_step "Step 6: ECR Stackの削除"

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

  delete_stack "${ECR_STACK}" "${REGION}"
else
  log_info "Step 6: ${ECR_STACK} はスキップ"
fi

# 7. Lambda@Edge support stack (us-east-1)
# Remove after the main stack (CloudFront). The first attempt can fail because replica removal lags,
# so this is best effort (a failure does not stop the whole script).
if [[ -n "${EDGE_STACK}" ]]; then
  if aws cloudformation describe-stacks --stack-name "${EDGE_STACK}" --region us-east-1 > /dev/null 2>&1; then
    log_step "Step 7: Lambda@Edge サポートスタックの削除 (us-east-1)"
    delete_stack "${EDGE_STACK}" "us-east-1" || {
      log_warn "Lambda@Edge サポートスタック(${EDGE_STACK})は数時間後に再度 ./scripts/destroy-v0.sh を実行して削除してください"
    }
  else
    log_info "Step 7: ${EDGE_STACK} はスキップ"
  fi
fi

# ===== Done =====
log_step "v0 環境削除完了"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  AI Persona System v0 環境削除完了                          ║${NC}"
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
