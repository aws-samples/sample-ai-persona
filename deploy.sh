#!/bin/bash
###############################################################################
# AI Persona System v1.0.0 - デプロイスクリプト（CloudShell 版）
#
# AWS CloudShell（ap-northeast-1）で実行する。1 回の実行で空の環境から
# Stage AIPersonaDev（Data・Edge・App の 3 スタック）を作る。
#
#   ① cdk deploy AIPersonaDev/Data AIPersonaDev/Edge（ECR を含む Data を先に作る）
#   ② コンテナイメージをビルドし、Data の ECR へ送る（タグは Git のコミットの短いハッシュ。
#      同じタグがあれば送らない）
#   ③ cdk deploy AIPersonaDev/App -c imageTag=<タグ>
#
# 使い方:
#   git clone https://github.com/aws-samples/sample-ai-persona.git
#   cd sample-ai-persona
#   ./deploy.sh
#
# オプション:
#   --restart-app      ①〜③をせず、ECS のタスクを入れ替えて完了を待つ
#                      （Secrets Manager の秘密の値を書き換えたあとに使う。鍵の入れ替え）
#   --image-tag TAG    ②をせず、ECR にある TAG のイメージで③を行う（前の版に戻すときに使う）
#   --region REGION    Data・App のリージョン（既定: ap-northeast-1。Edge は常に us-east-1）
#
# 事前に: ローカルの検査（pytest・ruff・mypy・CDK の検査）を通し、
#   cd cdk && npx cdk diff 'AIPersonaDev/*' -c imageTag=<タグ>
# でステートフルなリソースの置き換え（replace）がないことを確かめる。
#
# v0 のスタック（AIPersona-dev など）には触れない。v0 を使い続ける場合の手順は README を参照。
###############################################################################
set -euo pipefail

STAGE="AIPersonaDev"
REGION="ap-northeast-1"
EDGE_REGION="us-east-1"
RESTART_APP=false
IMAGE_TAG_OVERRIDE=""
UNSET_IMAGE_TAG="unset"

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
  cat <<'USAGE'
使い方: ./deploy.sh [オプション]
  --restart-app      ①〜③をせず、ECS のタスクを入れ替えて完了を待つ
                     （Secrets Manager の秘密の値を書き換えたあとに使う）
  --image-tag TAG    イメージのビルドと送信をせず、ECR にある TAG で App をデプロイする
                     （前の版に戻すときに使う）
  --region REGION    Data・App のリージョン（既定: ap-northeast-1。Edge は常に us-east-1）
  -h, --help         この説明を出す
USAGE
}

# ===== 引数 =====
while [[ $# -gt 0 ]]; do
  case $1 in
    --restart-app) RESTART_APP=true; shift ;;
    --image-tag)
      [[ $# -ge 2 ]] || { log_error "--image-tag にはタグを指定してください"; exit 1; }
      IMAGE_TAG_OVERRIDE="$2"; shift 2 ;;
    --region)
      [[ $# -ge 2 ]] || { log_error "--region にはリージョンを指定してください"; exit 1; }
      REGION="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "不明なオプション: $1"; usage; exit 1 ;;
  esac
done

if [[ "${IMAGE_TAG_OVERRIDE}" == "${UNSET_IMAGE_TAG}" ]]; then
  log_error "イメージのタグが ${UNSET_IMAGE_TAG} です。実在するタグを指定してください"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}"
if [[ ! -f "${PROJECT_ROOT}/cdk/bin/app.ts" ]]; then
  log_error "CDK プロジェクトが見つかりません。プロジェクトのルートで実行してください"
  exit 1
fi
cd "${PROJECT_ROOT}"

# ===== Step 0: 作業ツリーとイメージのタグ =====
log_step "Step 0: 作業ツリーとイメージのタグ"

# 汚れた作業ツリーからビルドすると、同じタグに別の中身のイメージができる（R-10）
if [[ "${RESTART_APP}" == "false" && -n "$(git status --porcelain)" ]]; then
  log_error "作業ツリーに未コミットの変更があります。コミットするか退避してから実行してください"
  git status --short
  exit 1
fi

IMAGE_TAG="${IMAGE_TAG_OVERRIDE:-$(git rev-parse --short HEAD)}"
log_info "イメージのタグ: ${IMAGE_TAG}"

stack_output() {
  # stack_output <スタック名> <出力名>
  aws cloudformation describe-stacks \
    --stack-name "$1" --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" \
    --output text
}

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
log_info "AWS アカウント: ${ACCOUNT_ID}"
log_info "リージョン: ${REGION}（Edge: ${EDGE_REGION}）"

# ===== --restart-app: タスクの入れ替えだけ =====
if [[ "${RESTART_APP}" == "true" ]]; then
  log_step "タスクの入れ替え（--restart-app）"
  CLUSTER=$(stack_output "${STAGE}-App" ClusterName)
  SERVICE=$(stack_output "${STAGE}-App" ServiceName)
  aws ecs update-service --cluster "${CLUSTER}" --service "${SERVICE}" \
    --force-new-deployment --region "${REGION}" > /dev/null
  log_info "入れ替えの完了を待っています（数分かかります）..."
  aws ecs wait services-stable --cluster "${CLUSTER}" --services "${SERVICE}" --region "${REGION}"
  log_info "タスクの入れ替えが完了しました"
  exit 0
fi

if [[ -z "${IMAGE_TAG_OVERRIDE}" ]] && ! docker info > /dev/null 2>&1; then
  log_error "Docker が使えません。CloudShell で実行してください"
  exit 1
fi

# ===== Step 1: CDK の準備 =====
log_step "Step 1: CDK の依存のインストールと bootstrap"
cd "${PROJECT_ROOT}/cdk"
npm ci
for target_region in "${REGION}" "${EDGE_REGION}"; do
  npx cdk bootstrap "aws://${ACCOUNT_ID}/${target_region}" --region "${target_region}"
done

CDK_CONTEXT=(-c "imageTag=${IMAGE_TAG}" -c "region=${REGION}")

# ===== Step 2: ① Data・Edge =====
log_step "Step 2: Data・Edge スタック（① テーブル・ECR・Cognito・WAF）"
npx cdk deploy "${STAGE}/Data" "${STAGE}/Edge" --require-approval never "${CDK_CONTEXT[@]}"

ECR_URI=$(stack_output "${STAGE}-Data" EcrRepositoryUri)
ECR_REPOSITORY="${ECR_URI#*/}"
log_info "ECR: ${ECR_URI}"

image_exists() {
  aws ecr describe-images --repository-name "${ECR_REPOSITORY}" \
    --image-ids "imageTag=${IMAGE_TAG}" --region "${REGION}" > /dev/null 2>&1
}

# ===== Step 3: ② イメージ =====
log_step "Step 3: コンテナイメージ（②）"
if [[ -n "${IMAGE_TAG_OVERRIDE}" ]]; then
  if ! image_exists; then
    log_error "ECR にタグ ${IMAGE_TAG} のイメージがありません"
    exit 1
  fi
  log_info "指定されたタグ ${IMAGE_TAG} のイメージを使います（ビルドしない）"
elif image_exists; then
  log_info "タグ ${IMAGE_TAG} のイメージは送信済みです（タグは不変のため送らない）"
else
  cd "${PROJECT_ROOT}"
  aws ecr get-login-password --region "${REGION}" | \
    docker login --username AWS --password-stdin "${ECR_URI%%/*}"
  docker build -t "${ECR_URI}:${IMAGE_TAG}" .
  docker push "${ECR_URI}:${IMAGE_TAG}"
  log_info "イメージを送りました: ${ECR_URI}:${IMAGE_TAG}"
fi

# ===== Step 4: ③ App =====
log_step "Step 4: App スタック（③ Fargate・内部 ALB・CloudFront）"
if [[ "${IMAGE_TAG}" == "${UNSET_IMAGE_TAG}" ]]; then
  log_error "イメージのタグが ${UNSET_IMAGE_TAG} のため App スタックをデプロイしません"
  exit 1
fi
cd "${PROJECT_ROOT}/cdk"
npx cdk deploy "${STAGE}/App" --require-approval never "${CDK_CONTEXT[@]}"

# ===== 完了 =====
APP_URL=$(stack_output "${STAGE}-App" AppUrl)
USER_POOL_ID=$(stack_output "${STAGE}-Data" UserPoolId)

log_step "デプロイ完了"
echo ""
echo -e "  アプリの URL:     ${BLUE}${APP_URL}${NC}"
echo -e "  ヘルスチェック:   ${BLUE}${APP_URL}/health${NC}"
echo -e "  User Pool ID:     ${USER_POOL_ID}"
echo -e "  イメージのタグ:   ${IMAGE_TAG}"
echo ""
echo "初めてのときは、動作確認用の admin を 1 人作ってください:"
echo ""
echo "  aws cognito-idp admin-create-user --region ${REGION} \\"
echo "    --user-pool-id ${USER_POOL_ID} --username <メールアドレス> \\"
echo "    --user-attributes Name=email,Value=<メールアドレス> Name=email_verified,Value=true"
echo "  aws cognito-idp admin-add-user-to-group --region ${REGION} \\"
echo "    --user-pool-id ${USER_POOL_ID} --username <メールアドレス> --group-name role:admin"
echo ""
echo "  （仮のパスワードがメールで届きます。初回ログインで新しいパスワードを設定します）"
echo ""
echo "再デプロイ（コードを更新したとき）: ./deploy.sh"
echo "前の版に戻す:                       ./deploy.sh --image-tag <前のタグ>"
echo "秘密を書き換えたあと:               ./deploy.sh --restart-app"
