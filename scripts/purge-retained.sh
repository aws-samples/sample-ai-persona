#!/bin/bash
###############################################################################
# AI Persona System v1.0.0 - script to purge RETAIN resources (CloudShell)
#
# Purges the RETAIN resources left after ./destroy.sh removes the stacks (DynamoDB, KMS keys, S3,
# Cognito User Pool, ECR, AgentCore Memory, Secrets Manager, log groups, Step Functions).
# This cannot be undone.
#
# This is separate from stack removal because CloudShell sessions expire after 20-30 minutes of
# inactivity, so waiting for stack removal could end the session before the data is purged.
# Leftover resources are re-collected from the removed stacks (CloudFormation keeps their records
# for 90 days). Resources already gone are skipped, so after an interruption rerunning the same command resumes.
#
# Targets the Stage's removed stacks (all within 90 days): resources left by RETAIN (DELETE_SKIPPED)
# that still exist. If any stack of the Stage still exists, it does nothing (to protect a running
# environment's data). It always asks for the Stage name before purging.
#
# Usage:
#   ./scripts/purge-retained.sh             # purge data of the public Stage (AIPersonaDev)
#   ./scripts/purge-retained.sh --closed    # purge data of the closed Stage (AIPersonaDevClosed)
#
# Options:
#   --closed          Target the closed Stage (AIPersonaDevClosed)
#   --region REGION   Region for Data and App (default: ap-northeast-1; Edge is always us-east-1)
###############################################################################
set -euo pipefail

STAGE="AIPersonaDev"
CLOSED_STAGE="AIPersonaDevClosed"
REGION="ap-northeast-1"
EDGE_REGION="us-east-1"
CLOSED=false

# Secrets Manager secrets are not removed immediately; they stay recoverable for this many days
SECRET_RECOVERY_DAYS=7
# KMS keys cannot be removed immediately. Deletion is scheduled and can be cancelled for this many days (7 is the KMS minimum)
KMS_PENDING_DAYS=7

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "\n${BLUE}========================================${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}========================================${NC}"; }

usage() {
  cat <<'EOF'
使い方: ./scripts/purge-retained.sh [オプション]
  ./destroy.sh でスタックを削除したあとに残る RETAIN のリソース（データ）を削除します。元に戻せません。
  削除済みのスタック（90 日以内）から対象を探し、すでに消えたものは飛ばします。
  途中で止まっても、同じコマンドを再実行すれば続きから削除できます。

  --closed          閉域の Stage（AIPersonaDevClosed）を対象にする（既定は公開の AIPersonaDev）
  --region REGION   Data・App のリージョン（既定: ap-northeast-1。Edge は常に us-east-1）
  -h, --help        この説明を表示

Stage のスタックが残っているときは何もしません。先に ./destroy.sh でスタックを削除してください。
削除の前に、Stage 名の入力による確認を必ず求めます。
EOF
}

# ===== Argument parsing (validated before touching AWS) =====
while [[ $# -gt 0 ]]; do
  case $1 in
    --closed) CLOSED=true; shift ;;
    --region)
      [[ $# -ge 2 ]] || { log_error "--region にはリージョンを指定してください"; exit 1; }
      REGION="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "不明なオプション: $1"; usage; exit 1 ;;
  esac
done

if [[ "${CLOSED}" == "true" ]]; then
  TARGET_STAGE="${CLOSED_STAGE}"
else
  TARGET_STAGE="${STAGE}"
fi

# Stage stacks (name<TAB>region). The closed Stage has no Edge
STACKS=("${TARGET_STAGE}-App"$'\t'"${REGION}")
if [[ "${CLOSED}" == "false" ]]; then
  STACKS+=("${TARGET_STAGE}-Edge"$'\t'"${EDGE_REGION}")
fi
STACKS+=("${TARGET_STAGE}-Data"$'\t'"${REGION}")

# ===== Environment checks =====
log_step "Step 0: 環境チェック"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
log_info "AWS アカウント: ${ACCOUNT_ID}"
log_info "リージョン: ${REGION}"
log_info "Stage: ${TARGET_STAGE}"

for entry in "${STACKS[@]}"; do
  IFS=$'\t' read -r name region <<< "${entry}"
  if aws cloudformation describe-stacks --stack-name "${name}" --region "${region}" > /dev/null 2>&1; then
    log_error "${name}（${region}）がまだ存在します。動いている環境のデータは削除しません。"
    log_error "先に ./destroy.sh$([[ "${CLOSED}" == "true" ]] && echo " --closed") でスタックを削除してから再実行してください。"
    exit 1
  fi
done

# ===== Re-collect RETAIN resources from the removed stacks =====
log_step "Step 1: 削除済みのスタックから残ったリソースを探す"
# One line = "<region>\t<type>\t<physical ID>"
FOUND=()
for entry in "${STACKS[@]}"; do
  IFS=$'\t' read -r name region <<< "${entry}"
  stack_ids=$(aws cloudformation list-stacks --region "${region}" \
    --stack-status-filter DELETE_COMPLETE \
    --query "StackSummaries[?StackName=='${name}'].StackId" --output text)
  for stack_id in ${stack_ids}; do
    [[ -z "${stack_id}" || "${stack_id}" == "None" ]] && continue
    log_info "  削除済み: ${stack_id}"
    resources=$(aws cloudformation list-stack-resources --stack-name "${stack_id}" --region "${region}" \
      --query "StackResourceSummaries[?ResourceStatus=='DELETE_SKIPPED'].[ResourceType,PhysicalResourceId]" \
      --output text)
    while IFS=$'\t' read -r rtype rid; do
      [[ -z "${rtype}" || "${rtype}" == "None" || -z "${rid}" ]] && continue
      FOUND+=("${region}"$'\t'"${rtype}"$'\t'"${rid}")
    done <<< "${resources}"
  done
done

# Repeated re-creation can list the same resource in several removed stacks
UNIQUE=()
if [[ ${#FOUND[@]} -gt 0 ]]; then
  while IFS= read -r line; do
    UNIQUE+=("${line}")
  done < <(printf '%s\n' "${FOUND[@]}" | sort -u)
fi

# ===== Keep only what still exists =====
not_found() {
  # Whether an existence-check failure means "not found". Insufficient permission etc. is treated as existing, so the purge fails
  grep -qE 'NotFound|NoSuchBucket|\(404\)|DoesNotExist|does not exist' <<< "$1"
}

resource_exists() {
  # resource_exists <region> <type> <physical ID>. 0 = exists, 1 = gone
  local region="$1" rtype="$2" rid="$3" out
  case "${rtype}" in
    AWS::DynamoDB::GlobalTable)
      out=$(aws dynamodb describe-table --table-name "${rid}" --region "${region}" 2>&1 > /dev/null) ;;
    AWS::S3::Bucket)
      out=$(aws s3api head-bucket --bucket "${rid}" --region "${region}" 2>&1 > /dev/null) ;;
    AWS::Cognito::UserPool)
      out=$(aws cognito-idp describe-user-pool --user-pool-id "${rid}" --region "${region}" 2>&1 > /dev/null) ;;
    AWS::ECR::Repository)
      out=$(aws ecr describe-repositories --repository-names "${rid}" --region "${region}" 2>&1 > /dev/null) ;;
    AWS::BedrockAgentCore::Memory)
      out=$(aws bedrock-agentcore-control get-memory --memory-id "${rid##*/}" --region "${region}" 2>&1 > /dev/null) ;;
    AWS::StepFunctions::StateMachine)
      out=$(aws stepfunctions describe-state-machine --state-machine-arn "${rid}" --region "${region}" 2>&1 > /dev/null) ;;
    AWS::SecretsManager::Secret)
      # Deletion already scheduled (DeletedDate present): nothing left to remove
      out=$(aws secretsmanager describe-secret --secret-id "${rid}" --region "${region}" \
        --query 'DeletedDate' --output text 2>&1) || { not_found "${out}" && return 1; return 0; }
      [[ "${out}" == "None" ]] && return 0
      return 1 ;;
    AWS::KMS::Key)
      # Deletion already scheduled (PendingDeletion): nothing left to remove
      out=$(aws kms describe-key --key-id "${rid}" --region "${region}" \
        --query 'KeyMetadata.KeyState' --output text 2>&1) || { not_found "${out}" && return 1; return 0; }
      [[ "${out}" == "PendingDeletion" ]] && return 1
      return 0 ;;
    AWS::Logs::LogGroup)
      out=$(aws logs describe-log-groups --log-group-name-prefix "${rid}" --region "${region}" \
        --query "length(logGroups[?logGroupName=='${rid}'])" --output text 2>&1) || return 0
      [[ "${out}" == "0" ]] && return 1
      return 0 ;;
    *)
      return 0 ;;
  esac && return 0
  not_found "${out}" && return 1
  return 0
}

RETAINED=()
if [[ ${#UNIQUE[@]} -gt 0 ]]; then
  for entry in "${UNIQUE[@]}"; do
    IFS=$'\t' read -r region rtype rid <<< "${entry}"
    if resource_exists "${region}" "${rtype}" "${rid}"; then
      RETAINED+=("${entry}")
    else
      log_info "  削除済みのため飛ばす: ${rtype} ${rid}"
    fi
  done
fi

if [[ ${#RETAINED[@]} -eq 0 ]]; then
  log_info "削除する RETAIN のリソースはありません。"
  exit 0
fi

print_retained() {
  local entry region rtype rid
  for entry in "${RETAINED[@]}"; do
    IFS=$'\t' read -r region rtype rid <<< "${entry}"
    echo "  - ${rtype}  ${rid}  (${region})"
  done
}

# ===== Confirmation (cannot be skipped) =====
echo ""
echo -e "${RED}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${RED}║  警告: 以下のデータを完全に削除します。元に戻せません        ║${NC}"
echo -e "${RED}╚══════════════════════════════════════════════════════════════╝${NC}"
print_retained
echo ""
read -r -p "削除する Stage 名（${TARGET_STAGE}）を入力してください: " CONFIRM_STAGE || CONFIRM_STAGE=""
if [[ "${CONFIRM_STAGE}" != "${TARGET_STAGE}" ]]; then
  log_info "入力が一致しないためキャンセルしました（何も削除していません）。"
  exit 0
fi

# ===== Purge =====
empty_versioned_bucket() {
  # Remove all versions and delete markers 1000 at a time (a versioned bucket is not emptied by s3 rm).
  # delete-objects exits 0 even if individual deletions fail; failures appear only in the response Errors.
  # Missing them would loop on the same objects forever, so stop with the reason if there is even one.
  local bucket="$1" region="$2" payload errors
  while true; do
    payload=$(aws s3api list-object-versions --bucket "${bucket}" --region "${region}" \
      --max-keys 1000 --no-paginate --output json \
      --query '{Objects: [Versions || `[]`, DeleteMarkers || `[]`][].{Key: Key, VersionId: VersionId}, Quiet: `true`}')
    [[ -n "${payload}" ]] || return 1
    if ! grep -q '"Key"' <<< "${payload}"; then
      return 0
    fi
    errors=$(aws s3api delete-objects --bucket "${bucket}" --region "${region}" --delete "${payload}" \
      --query 'Errors[].[Code, Key, Message]' --output text) || return 1
    if [[ -n "${errors}" && "${errors}" != "None" ]]; then
      log_error "${bucket} のオブジェクトを削除できませんでした（先頭 10 件: コード キー 理由）"
      head -n 10 <<< "${errors}" | while IFS= read -r line; do log_error "  ${line}"; done
      log_error "権限・Object Lock・MFA Delete などの原因を取り除いてから再実行してください"
      return 1
    fi
  done
}

purge_resource() {
  # purge_resource <region> <type> <physical ID>
  # The caller checks the result in an if, so set -e does not apply. Chain the steps with && to stop on a failure.
  local region="$1" rtype="$2" rid="$3" domain
  case "${rtype}" in
    AWS::DynamoDB::GlobalTable)
      # TableV2 (AWS::DynamoDB::GlobalTable). The physical ID is the table name; the replica exists in this region only
      aws dynamodb update-table --table-name "${rid}" --region "${region}" \
        --no-deletion-protection-enabled > /dev/null &&
      aws dynamodb wait table-exists --table-name "${rid}" --region "${region}" &&
      aws dynamodb delete-table --table-name "${rid}" --region "${region}" > /dev/null &&
      aws dynamodb wait table-not-exists --table-name "${rid}" --region "${region}" ;;
    AWS::S3::Bucket)
      empty_versioned_bucket "${rid}" "${region}" &&
      aws s3api delete-bucket --bucket "${rid}" --region "${region}" ;;
    AWS::Cognito::UserPool)
      # A User Pool cannot be removed while a domain remains (normally already gone with the Data stack)
      domain=$(aws cognito-idp describe-user-pool --user-pool-id "${rid}" --region "${region}" \
        --query 'UserPool.Domain' --output text 2>/dev/null || echo "")
      if [[ -n "${domain}" && "${domain}" != "None" ]]; then
        aws cognito-idp delete-user-pool-domain --domain "${domain}" --user-pool-id "${rid}" \
          --region "${region}" || return 1
      fi
      # update-user-pool resets unspecified settings to defaults, which does not matter since it is removed right after
      aws cognito-idp update-user-pool --user-pool-id "${rid}" --region "${region}" \
        --deletion-protection INACTIVE &&
      aws cognito-idp delete-user-pool --user-pool-id "${rid}" --region "${region}" ;;
    AWS::ECR::Repository)
      aws ecr delete-repository --repository-name "${rid}" --region "${region}" --force > /dev/null ;;
    AWS::BedrockAgentCore::Memory)
      # The physical ID is an ARN (.../memory/<MemoryId>)
      aws bedrock-agentcore-control delete-memory --memory-id "${rid##*/}" --region "${region}" > /dev/null ;;
    AWS::SecretsManager::Secret)
      aws secretsmanager delete-secret --secret-id "${rid}" --region "${region}" \
        --recovery-window-in-days "${SECRET_RECOVERY_DAYS}" > /dev/null ;;
    AWS::Logs::LogGroup)
      aws logs delete-log-group --log-group-name "${rid}" --region "${region}" ;;
    AWS::StepFunctions::StateMachine)
      aws stepfunctions delete-state-machine --state-machine-arn "${rid}" --region "${region}" ;;
    AWS::KMS::Key)
      aws kms schedule-key-deletion --key-id "${rid}" --region "${region}" \
        --pending-window-in-days "${KMS_PENDING_DAYS}" > /dev/null ;;
    *)
      log_warn "削除方法が分からない種類です（スキップ）: ${rtype} ${rid}"
      return 1 ;;
  esac
}

log_step "Step 2: RETAIN のリソースの削除"
PURGE_FAILED=()
# Schedule KMS key deletion last, after removing the tables encrypted with it
for pass in others kms; do
  for entry in "${RETAINED[@]}"; do
    IFS=$'\t' read -r region rtype rid <<< "${entry}"
    entry_pass=others
    [[ "${rtype}" == "AWS::KMS::Key" ]] && entry_pass=kms
    [[ "${entry_pass}" == "${pass}" ]] || continue
    log_info "削除: ${rtype} ${rid} (${region})"
    # Do not stop on one failure; continue and report all at the end
    if ! purge_resource "${region}" "${rtype}" "${rid}"; then
      log_error "削除に失敗しました: ${rtype} ${rid}"
      PURGE_FAILED+=("${entry}")
    fi
  done
done

# ===== Done =====
log_step "RETAIN のリソースの削除完了"
echo "Secrets Manager の秘密は ${SECRET_RECOVERY_DAYS} 日間は復元でき、KMS キーは ${KMS_PENDING_DAYS} 日後に削除されます（それまでは取り消せます）。"
if [[ ${#PURGE_FAILED[@]} -gt 0 ]]; then
  echo ""
  log_error "削除できなかったリソース:"
  RETAINED=("${PURGE_FAILED[@]}")
  print_retained
  log_error "原因を取り除いてから ./scripts/purge-retained.sh$([[ "${CLOSED}" == "true" ]] && echo " --closed") を再実行すると、残りだけを削除します。"
  exit 1
fi
