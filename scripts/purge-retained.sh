#!/bin/bash
###############################################################################
# AI Persona System v1.0.0 - RETAIN のリソースの削除スクリプト（CloudShell 版）
#
# ./destroy.sh でスタックを消したあとに残る RETAIN のリソース（DynamoDB・KMS キー・S3・
# Cognito User Pool・ECR・AgentCore Memory・Secrets Manager・ロググループ・Step Functions）を
# 削除する。元に戻せない。
#
# スタックの削除とは別のスクリプトにしているのは、CloudShell は操作がないと 20〜30 分で
# セッションが切れ、スタックの削除を待つうちにデータの削除まで進まないため。
# 残ったリソースは、削除済みのスタック（削除から 90 日間は CloudFormation が記録を返す）から
# 取り直す。すでに消えたものは飛ばすので、途中で切れても同じコマンドを再実行すれば続きから消せる。
#
# 対象は Stage の削除済みスタック（90 日以内のすべて）で、RETAIN で残ったもの
# （DELETE_SKIPPED）のうち、まだ存在するもの。Stage のスタックが 1 つでも残っていれば
# 何もしない（動いている環境のデータを消さないため）。削除の前に必ず Stage 名の入力を求める。
#
# 使い方:
#   ./scripts/purge-retained.sh             # 公開の Stage（AIPersonaDev）のデータを削除
#   ./scripts/purge-retained.sh --closed    # 閉域の Stage（AIPersonaDevClosed）のデータを削除
#
# オプション:
#   --closed          閉域の Stage（AIPersonaDevClosed）を対象にする
#   --region REGION   Data・App のリージョン（既定: ap-northeast-1。Edge は常に us-east-1）
###############################################################################
set -euo pipefail

STAGE="AIPersonaDev"
CLOSED_STAGE="AIPersonaDevClosed"
REGION="ap-northeast-1"
EDGE_REGION="us-east-1"
CLOSED=false

# Secrets Manager の秘密は即時に消さず、この日数のあいだは復元できるようにする
SECRET_RECOVERY_DAYS=7
# KMS キーは即時に消せない。削除を予約し、この日数のあいだは取り消せる（7 は KMS の最小値）
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

# ===== 引数パース（AWS に触れる前に検査する） =====
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

# Stage のスタック（名前<TAB>リージョン）。閉域には Edge がない
STACKS=("${TARGET_STAGE}-App"$'\t'"${REGION}")
if [[ "${CLOSED}" == "false" ]]; then
  STACKS+=("${TARGET_STAGE}-Edge"$'\t'"${EDGE_REGION}")
fi
STACKS+=("${TARGET_STAGE}-Data"$'\t'"${REGION}")

# ===== 環境チェック =====
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

# ===== 削除済みのスタックから、RETAIN で残ったリソースを取り直す =====
log_step "Step 1: 削除済みのスタックから残ったリソースを探す"
# 1 行 = "<リージョン>\t<種類>\t<物理 ID>"
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

# 作り直しを繰り返すと、同じリソースが複数の削除済みスタックに載ることがある
UNIQUE=()
if [[ ${#FOUND[@]} -gt 0 ]]; then
  while IFS= read -r line; do
    UNIQUE+=("${line}")
  done < <(printf '%s\n' "${FOUND[@]}" | sort -u)
fi

# ===== まだ存在するものだけに絞る =====
not_found() {
  # 存在確認の失敗が「見つからない」によるものか。権限不足などは存在するものとして扱い、削除で失敗させる
  grep -qE 'NotFound|NoSuchBucket|\(404\)|DoesNotExist|does not exist' <<< "$1"
}

resource_exists() {
  # resource_exists <リージョン> <種類> <物理 ID>。0 = 存在する、1 = もうない
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
      # 削除を予約済み（DeletedDate あり）なら、もう消すものはない
      out=$(aws secretsmanager describe-secret --secret-id "${rid}" --region "${region}" \
        --query 'DeletedDate' --output text 2>&1) || { not_found "${out}" && return 1; return 0; }
      [[ "${out}" == "None" ]] && return 0
      return 1 ;;
    AWS::KMS::Key)
      # 削除を予約済み（PendingDeletion）なら、もう消すものはない
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

# ===== 確認（省けない） =====
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

# ===== 削除 =====
empty_versioned_bucket() {
  # 全バージョンと削除マーカーを 1000 件ずつ消す（バージョニング有効のバケットは s3 rm では空にならない）
  # delete-objects は個別の削除に失敗しても終了コード 0 を返し、失敗は応答の Errors にだけ載る。
  # 見落とすと同じオブジェクトを消し続けて止まらないため、1 件でもあれば理由を出して止める
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
  # purge_resource <リージョン> <種類> <物理 ID>
  # 呼び出し側が if で結果を見るため set -e は効かない。各手順を && でつなぎ、途中の失敗で止める
  local region="$1" rtype="$2" rid="$3" domain
  case "${rtype}" in
    AWS::DynamoDB::GlobalTable)
      # TableV2（AWS::DynamoDB::GlobalTable）。物理 ID はテーブル名で、レプリカはこのリージョンだけ
      aws dynamodb update-table --table-name "${rid}" --region "${region}" \
        --no-deletion-protection-enabled > /dev/null &&
      aws dynamodb wait table-exists --table-name "${rid}" --region "${region}" &&
      aws dynamodb delete-table --table-name "${rid}" --region "${region}" > /dev/null &&
      aws dynamodb wait table-not-exists --table-name "${rid}" --region "${region}" ;;
    AWS::S3::Bucket)
      empty_versioned_bucket "${rid}" "${region}" &&
      aws s3api delete-bucket --bucket "${rid}" --region "${region}" ;;
    AWS::Cognito::UserPool)
      # ドメインが残っていると User Pool は消せない（通常は Data スタックと一緒に消えている）
      domain=$(aws cognito-idp describe-user-pool --user-pool-id "${rid}" --region "${region}" \
        --query 'UserPool.Domain' --output text 2>/dev/null || echo "")
      if [[ -n "${domain}" && "${domain}" != "None" ]]; then
        aws cognito-idp delete-user-pool-domain --domain "${domain}" --user-pool-id "${rid}" \
          --region "${region}" || return 1
      fi
      # update-user-pool は指定しない設定を既定に戻すが、直後に消すため問題にならない
      aws cognito-idp update-user-pool --user-pool-id "${rid}" --region "${region}" \
        --deletion-protection INACTIVE &&
      aws cognito-idp delete-user-pool --user-pool-id "${rid}" --region "${region}" ;;
    AWS::ECR::Repository)
      aws ecr delete-repository --repository-name "${rid}" --region "${region}" --force > /dev/null ;;
    AWS::BedrockAgentCore::Memory)
      # 物理 ID は ARN（.../memory/<MemoryId>）
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
# KMS キーは、それで暗号化したテーブルを消してから最後に削除を予約する
for pass in others kms; do
  for entry in "${RETAINED[@]}"; do
    IFS=$'\t' read -r region rtype rid <<< "${entry}"
    entry_pass=others
    [[ "${rtype}" == "AWS::KMS::Key" ]] && entry_pass=kms
    [[ "${entry_pass}" == "${pass}" ]] || continue
    log_info "削除: ${rtype} ${rid} (${region})"
    # 1 件の失敗で止めず、残りを続けて最後にまとめて示す
    if ! purge_resource "${region}" "${rtype}" "${rid}"; then
      log_error "削除に失敗しました: ${rtype} ${rid}"
      PURGE_FAILED+=("${entry}")
    fi
  done
done

# ===== 完了 =====
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
