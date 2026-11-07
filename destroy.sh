#!/bin/bash
###############################################################################
# AI Persona System v1.0.0 - 環境削除スクリプト（CloudShell 版）
#
# deploy.sh で作った v1.0.0 の Stage のスタックを、依存の逆順で削除する。
#
#   公開（既定）: Stage AIPersonaDev
#     ① AIPersonaDev-App（Data・Edge を参照しているため最初）
#     ② AIPersonaDev-Edge（us-east-1。CloudFront の WAF）
#     ③ AIPersonaDev-Data
#   閉域（--closed）: Stage AIPersonaDevClosed
#     ① AIPersonaDevClosed-App ② AIPersonaDevClosed-Data（Edge はない）
#
# ステートフルなリソース（DynamoDB・KMS キー・S3・Cognito User Pool・ECR・AgentCore Memory・
# Secrets Manager・ロググループ・Step Functions）は全環境で RETAIN のため、スタックを
# 消しても AWS に残る。既定ではデータを消さず、残るリソースを最後に一覧で示す。
# 再デプロイすると新しいリソースが作られ、残ったリソースのデータは引き継がれない。
#
# --purge-retained を付けると、スタックの削除後に RETAIN のリソースも削除する
# （削除保護を外し、S3 は全バージョンを消してからバケットを消す。元に戻せない）。
# このときは --force を付けていても、Stage 名の入力による確認を必ず求める。
#
# v0 のスタック（AIPersona-dev など）には触れない。v0 の削除は scripts/destroy-v0.sh。
#
# 使い方:
#   ./destroy.sh                       # 公開の Stage を削除（データは残す）
#   ./destroy.sh --closed              # 閉域の Stage を削除（データは残す）
#   ./destroy.sh --purge-retained      # 公開の Stage とそのデータをすべて削除
#
# オプション:
#   --closed          閉域の Stage（AIPersonaDevClosed）を削除する
#   --region REGION   Data・App のリージョン（既定: ap-northeast-1。Edge は常に us-east-1）
#   --purge-retained  RETAIN のリソース（データ）も削除する
#   --force           スタック削除の確認プロンプトをスキップ（--purge-retained の確認は省けない）
###############################################################################
set -euo pipefail

STAGE="AIPersonaDev"
CLOSED_STAGE="AIPersonaDevClosed"
REGION="ap-northeast-1"
EDGE_REGION="us-east-1"
CLOSED=false
FORCE=false
PURGE_RETAINED=false

# スタックを消しても残る（RETAIN の）リソースの種類。削除前に物理 ID を控える。
# CDK に RETAIN の種類が増えたら、ここと purge_resource に足す（cdk/test/teardown-script.test.ts が検査する）
RETAINED_TYPES="'AWS::DynamoDB::GlobalTable','AWS::KMS::Key','AWS::S3::Bucket','AWS::Cognito::UserPool','AWS::ECR::Repository','AWS::BedrockAgentCore::Memory','AWS::SecretsManager::Secret','AWS::Logs::LogGroup','AWS::StepFunctions::StateMachine'"
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
使い方: ./destroy.sh [オプション]
  v1.0.0 の Stage のスタックを App → Edge → Data の順に削除します。
  既定では、データ（DynamoDB・S3・Cognito など）は RETAIN のため残ります。

  --closed          閉域の Stage（AIPersonaDevClosed）を削除する（既定は公開の AIPersonaDev）
  --region REGION   Data・App のリージョン（既定: ap-northeast-1。Edge は常に us-east-1）
  --purge-retained  スタックの削除後に RETAIN のリソース（データ）も削除する。元に戻せない。
                    --force を付けていても Stage 名の入力による確認を求める
  --force           スタック削除の確認プロンプトをスキップ
  -h, --help        この説明を表示

v0 のスタック（AIPersona-dev など）は削除しません。v0 は scripts/destroy-v0.sh で削除します。
EOF
}

# ===== 引数パース（AWS に触れる前に検査する） =====
while [[ $# -gt 0 ]]; do
  case $1 in
    --closed) CLOSED=true; shift ;;
    --region)
      [[ $# -ge 2 ]] || { log_error "--region にはリージョンを指定してください"; exit 1; }
      REGION="$2"; shift 2 ;;
    --purge-retained) PURGE_RETAINED=true; shift ;;
    --force) FORCE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "不明なオプション: $1"; usage; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "${SCRIPT_DIR}/cdk/bin/app.ts" ]]; then
  log_error "CDK プロジェクトが見つかりません。プロジェクトルートで実行してください。"
  exit 1
fi

if [[ "${CLOSED}" == "true" ]]; then
  TARGET_STAGE="${CLOSED_STAGE}"
else
  TARGET_STAGE="${STAGE}"
fi
APP_STACK="${TARGET_STAGE}-App"
EDGE_STACK="${TARGET_STAGE}-Edge"
DATA_STACK="${TARGET_STAGE}-Data"

# ===== 環境チェック =====
log_step "Step 0: 環境チェック"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
log_info "AWS アカウント: ${ACCOUNT_ID}"
log_info "リージョン: ${REGION}"
log_info "Stage: ${TARGET_STAGE}"

stack_exists() {
  # stack_exists <スタック名> <リージョン>
  aws cloudformation describe-stacks --stack-name "$1" --region "$2" > /dev/null 2>&1
}

# ===== 削除対象の確認（依存の逆順に並べる） =====
log_step "削除対象スタックの確認"

DELETE_NAMES=()
DELETE_REGIONS=()
add_if_exists() {
  local name="$1" region="$2"
  if stack_exists "${name}" "${region}"; then
    log_info "  存在: ${name} (${region})"
    DELETE_NAMES+=("${name}")
    DELETE_REGIONS+=("${region}")
  else
    log_info "  なし: ${name} (${region}) (スキップ)"
  fi
}

add_if_exists "${APP_STACK}" "${REGION}"
if [[ "${CLOSED}" == "false" ]]; then
  add_if_exists "${EDGE_STACK}" "${EDGE_REGION}"
fi
add_if_exists "${DATA_STACK}" "${REGION}"

if [[ ${#DELETE_NAMES[@]} -eq 0 ]]; then
  log_info "削除対象のスタックがありません。"
  if [[ "${PURGE_RETAINED}" == "true" ]]; then
    log_warn "スタックがないため、RETAIN のリソースは特定できません。残っていればコンソールか CLI で削除してください。"
  fi
  exit 0
fi

# ===== 残るリソースを控える（スタックを消すと一覧が取れなくなるため先に取る） =====
# 1 行 = "<リージョン>\t<種類>\t<物理 ID>"
RETAINED=()
for i in "${!DELETE_NAMES[@]}"; do
  resources=$(aws cloudformation describe-stack-resources \
    --stack-name "${DELETE_NAMES[$i]}" --region "${DELETE_REGIONS[$i]}" \
    --query "StackResources[?contains([${RETAINED_TYPES}], ResourceType)].[ResourceType,PhysicalResourceId]" \
    --output text 2>/dev/null || echo "")
  while IFS=$'\t' read -r rtype rid; do
    [[ -z "${rtype}" || "${rtype}" == "None" || -z "${rid}" ]] && continue
    RETAINED+=("${DELETE_REGIONS[$i]}"$'\t'"${rtype}"$'\t'"${rid}")
  done <<< "${resources}"
done

print_retained() {
  local entry region rtype rid
  for entry in "${RETAINED[@]}"; do
    IFS=$'\t' read -r region rtype rid <<< "${entry}"
    echo "  - ${rtype}  ${rid}  (${region})"
  done
}

# ===== 確認プロンプト（何かを消す前にすべての確認を済ませる） =====
if [[ "${FORCE}" == "false" ]]; then
  echo ""
  echo -e "${RED}警告: 以下のスタックをこの順で削除します${NC}"
  for i in "${!DELETE_NAMES[@]}"; do
    echo "  $((i + 1)). ${DELETE_NAMES[$i]} (${DELETE_REGIONS[$i]})"
  done
  echo ""
  if [[ "${PURGE_RETAINED}" == "false" ]]; then
    echo -e "${YELLOW}データ（DynamoDB・S3・Cognito など）は RETAIN のため削除されず残ります。${NC}"
    echo -e "${YELLOW}スタックを消すとアプリは使えなくなり、再デプロイしても残ったデータは引き継がれません。${NC}"
    echo ""
  fi
  read -r -p "本当に削除しますか？ (yes/no): " CONFIRM
  if [[ "${CONFIRM}" != "yes" ]]; then
    log_info "キャンセルしました。"
    exit 0
  fi
fi

if [[ "${PURGE_RETAINED}" == "true" ]]; then
  echo ""
  echo -e "${RED}╔══════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${RED}║  警告: --purge-retained によりデータも完全に削除します       ║${NC}"
  echo -e "${RED}║  DynamoDB・S3（全バージョン）・Cognito の利用者・ECR の      ║${NC}"
  echo -e "${RED}║  イメージ・AgentCore Memory などは元に戻せません             ║${NC}"
  echo -e "${RED}╚══════════════════════════════════════════════════════════════╝${NC}"
  if [[ ${#RETAINED[@]} -eq 0 ]]; then
    echo "  （RETAIN のリソースは見つかりませんでした）"
  else
    print_retained
  fi
  echo ""
  read -r -p "削除する Stage 名（${TARGET_STAGE}）を入力してください: " CONFIRM_STAGE
  if [[ "${CONFIRM_STAGE}" != "${TARGET_STAGE}" ]]; then
    log_info "入力が一致しないためキャンセルしました（何も削除していません）。"
    exit 0
  fi
fi

delete_stack() {
  # delete_stack <スタック名> <リージョン>
  local stack_name="$1" region="$2"
  aws cloudformation delete-stack --stack-name "${stack_name}" --region "${region}"
  log_info "${stack_name} の削除を開始しました。完了を待機中..."
  if ! aws cloudformation wait stack-delete-complete --stack-name "${stack_name}" --region "${region}"; then
    log_error "${stack_name} の削除に失敗またはタイムアウトしました"
    log_error "CloudFormation のコンソールでイベントを確かめ、原因を取り除いてから再実行してください"
    log_error "（後続のスタックと RETAIN のリソースは依存しているため削除しません）"
    exit 1
  fi
  log_info "${stack_name} を削除しました"
}

# ===== RETAIN のリソースの削除（--purge-retained） =====
empty_versioned_bucket() {
  # 全バージョンと削除マーカーを 1000 件ずつ消す（バージョニング有効のバケットは s3 rm では空にならない）
  local bucket="$1" region="$2" payload
  while true; do
    payload=$(aws s3api list-object-versions --bucket "${bucket}" --region "${region}" \
      --max-keys 1000 --no-paginate --output json \
      --query '{Objects: [Versions || `[]`, DeleteMarkers || `[]`][].{Key: Key, VersionId: VersionId}, Quiet: `true`}')
    [[ -n "${payload}" ]] || return 1
    if ! grep -q '"Key"' <<< "${payload}"; then
      return 0
    fi
    aws s3api delete-objects --bucket "${bucket}" --region "${region}" --delete "${payload}" > /dev/null ||
      return 1
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

# ===== 削除実行 =====
for i in "${!DELETE_NAMES[@]}"; do
  log_step "Step $((i + 1)): ${DELETE_NAMES[$i]} の削除 (${DELETE_REGIONS[$i]})"
  delete_stack "${DELETE_NAMES[$i]}" "${DELETE_REGIONS[$i]}"
done

PURGE_FAILED=()
if [[ "${PURGE_RETAINED}" == "true" && ${#RETAINED[@]} -gt 0 ]]; then
  log_step "Step $((${#DELETE_NAMES[@]} + 1)): RETAIN のリソースの削除"
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
fi

# ===== 完了 =====
log_step "環境削除完了"

echo "削除したスタック:"
for name in "${DELETE_NAMES[@]}"; do
  echo "  ✓ ${name}"
done
if [[ "${PURGE_RETAINED}" == "true" ]]; then
  if [[ ${#RETAINED[@]} -gt 0 ]]; then
    echo ""
    echo "RETAIN のリソースも削除しました。"
    echo "Secrets Manager の秘密は ${SECRET_RECOVERY_DAYS} 日間は復元でき、KMS キーは ${KMS_PENDING_DAYS} 日後に削除されます（それまでは取り消せます）。"
  fi
  if [[ ${#PURGE_FAILED[@]} -gt 0 ]]; then
    echo ""
    log_error "削除できなかったリソース（コンソールか CLI で確かめてください）:"
    RETAINED=("${PURGE_FAILED[@]}")
    print_retained
    exit 1
  fi
elif [[ ${#RETAINED[@]} -gt 0 ]]; then
  echo ""
  echo -e "${YELLOW}RETAIN のため残っているリソース（種類 / 物理 ID）:${NC}"
  print_retained
  echo ""
  echo "不要なら ./destroy.sh --purge-retained を使うか、中身を確かめたうえで手で削除してください。"
  echo "DynamoDB テーブルと Cognito User Pool は削除保護が有効です（外してから削除します）。"
fi
echo ""
echo "v0 のスタックには触れていません。v0 を削除する場合は scripts/destroy-v0.sh を使います。"
