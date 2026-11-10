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
# 消しても AWS に残る。このスクリプトはデータを消さず、残るリソースを最後に一覧で示す。
# 再デプロイすると新しいリソースが作られ、残ったリソースのデータは引き継がれない。
#
# データも消すときは、スタックを消したあとに scripts/purge-retained.sh を実行する。
# 別のスクリプトにしているのは、CloudShell は操作がないと 20〜30 分でセッションが切れ、
# スタックの削除を待つうちにデータの削除まで進まないため。
#
# v0 のスタック（AIPersona-dev など）には触れない。v0 の削除は scripts/destroy-v0.sh。
#
# 使い方:
#   ./destroy.sh                       # 公開の Stage を削除（データは残す）
#   ./destroy.sh --closed              # 閉域の Stage を削除（データは残す）
#
# オプション:
#   --closed          閉域の Stage（AIPersonaDevClosed）を削除する
#   --region REGION   Data・App のリージョン（既定: ap-northeast-1。Edge は常に us-east-1）
#   --force           スタック削除の確認プロンプトをスキップ
###############################################################################
set -euo pipefail

STAGE="AIPersonaDev"
CLOSED_STAGE="AIPersonaDevClosed"
REGION="ap-northeast-1"
EDGE_REGION="us-east-1"
CLOSED=false
FORCE=false

# スタックを消しても残る（RETAIN の）リソースの種類。削除前に物理 ID を控えて最後に示す。
# CDK に RETAIN の種類が増えたら、ここと scripts/purge-retained.sh の purge_resource に足す
# （cdk/test/teardown-script.test.ts が検査する）
RETAINED_TYPES="'AWS::DynamoDB::GlobalTable','AWS::KMS::Key','AWS::S3::Bucket','AWS::Cognito::UserPool','AWS::ECR::Repository','AWS::BedrockAgentCore::Memory','AWS::SecretsManager::Secret','AWS::Logs::LogGroup','AWS::StepFunctions::StateMachine'"

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
  データ（DynamoDB・S3・Cognito など）は RETAIN のため残ります。

  --closed          閉域の Stage（AIPersonaDevClosed）を削除する（既定は公開の AIPersonaDev）
  --region REGION   Data・App のリージョン（既定: ap-northeast-1。Edge は常に us-east-1）
  --force           スタック削除の確認プロンプトをスキップ
  -h, --help        この説明を表示

データも消すときは、スタックを削除したあとに ./scripts/purge-retained.sh を実行します。
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
    --purge-retained)
      log_error "--purge-retained は廃止しました。スタックを削除したあとに ./scripts/purge-retained.sh を実行してください。"
      exit 1 ;;
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
  log_info "残ったデータを消すときは ./scripts/purge-retained.sh$([[ "${CLOSED}" == "true" ]] && echo " --closed") を実行してください。"
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
  echo -e "${YELLOW}データ（DynamoDB・S3・Cognito など）は RETAIN のため削除されず残ります。${NC}"
  echo -e "${YELLOW}スタックを消すとアプリは使えなくなり、再デプロイしても残ったデータは引き継がれません。${NC}"
  echo ""
  read -r -p "本当に削除しますか？ (yes/no): " CONFIRM
  if [[ "${CONFIRM}" != "yes" ]]; then
    log_info "キャンセルしました。"
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
    log_error "（後続のスタックは依存しているため削除しません）"
    exit 1
  fi
  log_info "${stack_name} を削除しました"
}

# ===== 削除実行 =====
for i in "${!DELETE_NAMES[@]}"; do
  log_step "Step $((i + 1)): ${DELETE_NAMES[$i]} の削除 (${DELETE_REGIONS[$i]})"
  delete_stack "${DELETE_NAMES[$i]}" "${DELETE_REGIONS[$i]}"
done

# ===== 完了 =====
log_step "環境削除完了"

echo "削除したスタック:"
for name in "${DELETE_NAMES[@]}"; do
  echo "  ✓ ${name}"
done
if [[ ${#RETAINED[@]} -gt 0 ]]; then
  echo ""
  echo -e "${YELLOW}RETAIN のため残っているリソース（種類 / 物理 ID）:${NC}"
  print_retained
  echo ""
  echo "不要なら ./scripts/purge-retained.sh$([[ "${CLOSED}" == "true" ]] && echo " --closed") で削除します（Stage 名の入力で確認します）。"
  echo "このあとセッションが切れても、削除済みのスタックから対象を探し直すため、いつでも実行できます。"
fi
echo ""
echo "v0 のスタックには触れていません。v0 を削除する場合は scripts/destroy-v0.sh を使います。"
