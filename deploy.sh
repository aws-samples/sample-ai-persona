#!/bin/bash
###############################################################################
# AI Persona System v1.0.0 - deploy script (CloudShell)
#
# Run in AWS CloudShell (ap-northeast-1). A single run builds the Stage AIPersonaDev
# (Data, Edge and App stacks) from an empty environment.
#
#   1. cdk deploy AIPersonaDev/Data AIPersonaDev/Edge (Data, which holds the ECR, comes first)
#   2. Build the container image and push it to the Data ECR (tag = short Git commit hash;
#      skipped if the tag already exists)
#   3. cdk deploy AIPersonaDev/App -c imageTag=<tag>
#
# Usage:
#   git clone https://github.com/aws-samples/sample-ai-persona.git
#   cd sample-ai-persona
#   ./deploy.sh
#
# Options:
#   --restart-app      Skip steps 1-3; replace the ECS tasks and wait for completion
#                      (use after rewriting a Secrets Manager secret value, i.e. key rotation)
#   --image-tag TAG    Skip step 2 and run step 3 with the image TAG already in ECR (for rollback)
#   --region REGION    Region for Data and App (default: ap-northeast-1; Edge is always us-east-1)
#   --inference-scope global|jp
#                      Inference scope; affects only the Stage being deployed (public: -c inferenceScope=<value>,
#                      --closed: -c closedInferenceScope=<value>). Defaults to inferenceScope in
#                      parameters.ts, else global. To keep using jp, set it in parameters.ts (cdk/README.md)
#
# Closed network (Stage AIPersonaDevClosed; Data and App stacks; does not touch the public Stage):
#   ./deploy.sh --closed --certificate-arn <ACM certificate ARN> --domain-name <FQDN> \
#     [--allowed-cidrs <CIDR,CIDR>] [--create-test-environment] [--endpoint-az-count <1|2>] \
#     [--create-resolver-endpoint --resolver-allowed-cidrs <internal DNS CIDR,CIDR>] \
#     [--remove-resolver-endpoint]
#   The script stops before changing AWS if the deployed App has a Resolver but
#   --create-resolver-endpoint is missing, or the endpoints span 2 AZs but --endpoint-az-count is missing.
#   1. cdk deploy AIPersonaDevClosed/Data (no us-east-1 bootstrap and no Edge)
#   2. Push the image to the closed Data ECR  3. cdk deploy AIPersonaDevClosed/App -c imageTag=<tag>
#   --image-tag, --restart-app and --region also work with --closed (they target the closed Stage).
#   Without --certificate-arn and --domain-name, usage is printed and the script exits before touching AWS.
#
# Before running: pass the local checks (pytest, ruff, mypy, CDK checks) and run
#   cd cdk && npx cdk diff 'AIPersonaDev/*' -c imageTag=<tag>
# to confirm no stateful resource is replaced.
#
# Does not touch v0 stacks (AIPersona-dev etc.). See the README for keeping v0 running.
###############################################################################
set -euo pipefail

STAGE="AIPersonaDev"
CLOSED_STAGE="AIPersonaDevClosed"
REGION="ap-northeast-1"
EDGE_REGION="us-east-1"
RESTART_APP=false
IMAGE_TAG_OVERRIDE=""
UNSET_IMAGE_TAG="unset"
CLOSED=false
CERTIFICATE_ARN=""
DOMAIN_NAME=""
ALLOWED_CIDRS=""
CREATE_TEST_ENVIRONMENT=false
NEMOTRON_DATASET_KEY=""
ENDPOINT_AZ_COUNT=""
ENDPOINT_AZ_COUNT_GIVEN=false
CREATE_RESOLVER_ENDPOINT=false
RESOLVER_ALLOWED_CIDRS=""
REMOVE_RESOLVER_ENDPOINT=false
CLOSED_OPTION_GIVEN=""
INFERENCE_SCOPE=""
INFERENCE_SCOPE_GIVEN=false

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
  --inference-scope global|jp
                     推論の範囲（デプロイする Stage だけに効く。--closed と一緒なら閉域だけ）。
                     jp は国内（東京・大阪）の推論プロファイルだけを使う。
                     省略時は parameters.ts の inferenceScope か global。jp を続けて使うなら
                     parameters.ts に書く（付け忘れると global に戻る。cdk/README.md「推論の範囲」）
  -h, --help         この説明を出す

閉域構成（Stage AIPersonaDevClosed。公開の Stage には触れない）:
  --closed                    閉域の Stage を Data → イメージ → App の順にデプロイする
                              （us-east-1 の bootstrap と Edge はしない）
  --certificate-arn ARN       内部 ALB の証明書（Data・App と同じリージョンの ACM）。--closed で必須
  --domain-name FQDN          利用者が開く FQDN（例 ai-persona.example.internal）。--closed で必須
  --allowed-cidrs CIDRS       ALB に届いてよい送り元の CIDR（カンマ区切り。既定: なし）
  --create-test-environment   検証用の Windows EC2（Fleet Manager で入る）も作る
  --nemotron-dataset-key KEY  事前に配置する Nemotron の Parquet のキー（アプリのバケットの中。
                              既定: persona-dataset/nemotron-personas-japan.parquet）
  --endpoint-az-count 1|2     インターフェイス型 VPC エンドポイントを置く AZ の数（既定: parameters.ts の
                              値。dev は 1）。1 ではタスクと検証用 EC2 も同じ AZ に置く（ALB は 2 AZ）
  --create-resolver-endpoint  社内 DNS から転送を受ける Route 53 Resolver の Inbound Endpoint を作る
                              （既定: 作らない。--resolver-allowed-cidrs が要る）
  --resolver-allowed-cidrs CIDRS
                              Resolver に問い合わせてよい社内 DNS の CIDR（カンマ区切り。TCP・UDP 53）
  --remove-resolver-endpoint  デプロイ済みの Resolver を消す（付けないと、Resolver があるのに
                              --create-resolver-endpoint がない再デプロイは止まる）
  再デプロイで Resolver が消えたりエンドポイントの AZ が 2 から減ったりする引数のときは、
  AWS を変える前に止まる（減らすときは --endpoint-az-count 1 を明示する）
  --restart-app・--image-tag・--region は --closed と一緒に使える（対象は閉域の Stage）
  許可する CIDR か検証用 EC2 のどちらかが要る（どちらもなければ CDK の synth で止まる）

例:
  ./deploy.sh --closed --certificate-arn arn:aws:acm:ap-northeast-1:<アカウント>:certificate/<ID> \
    --domain-name ai-persona.example.internal --create-test-environment
USAGE
}

# ===== Arguments =====
while [[ $# -gt 0 ]]; do
  case $1 in
    --restart-app) RESTART_APP=true; shift ;;
    --image-tag)
      [[ $# -ge 2 ]] || { log_error "--image-tag にはタグを指定してください"; exit 1; }
      IMAGE_TAG_OVERRIDE="$2"; shift 2 ;;
    --region)
      [[ $# -ge 2 ]] || { log_error "--region にはリージョンを指定してください"; exit 1; }
      REGION="$2"; shift 2 ;;
    --inference-scope)
      [[ $# -ge 2 ]] || { log_error "--inference-scope には global か jp を指定してください"; exit 1; }
      INFERENCE_SCOPE="$2"; INFERENCE_SCOPE_GIVEN=true; shift 2 ;;
    --closed) CLOSED=true; shift ;;
    --certificate-arn)
      [[ $# -ge 2 ]] || { log_error "--certificate-arn には証明書の ARN を指定してください"; exit 1; }
      CERTIFICATE_ARN="$2"; CLOSED_OPTION_GIVEN="$1"; shift 2 ;;
    --domain-name)
      [[ $# -ge 2 ]] || { log_error "--domain-name には FQDN を指定してください"; exit 1; }
      DOMAIN_NAME="$2"; CLOSED_OPTION_GIVEN="$1"; shift 2 ;;
    --allowed-cidrs)
      [[ $# -ge 2 ]] || { log_error "--allowed-cidrs には CIDR（カンマ区切り）を指定してください"; exit 1; }
      ALLOWED_CIDRS="$2"; CLOSED_OPTION_GIVEN="$1"; shift 2 ;;
    --create-test-environment) CREATE_TEST_ENVIRONMENT=true; CLOSED_OPTION_GIVEN="$1"; shift ;;
    --nemotron-dataset-key)
      [[ $# -ge 2 ]] || { log_error "--nemotron-dataset-key には S3 のキーを指定してください"; exit 1; }
      NEMOTRON_DATASET_KEY="$2"; CLOSED_OPTION_GIVEN="$1"; shift 2 ;;
    --endpoint-az-count)
      [[ $# -ge 2 ]] || { log_error "--endpoint-az-count には 1 か 2 を指定してください"; exit 1; }
      ENDPOINT_AZ_COUNT="$2"; ENDPOINT_AZ_COUNT_GIVEN=true; CLOSED_OPTION_GIVEN="$1"; shift 2 ;;
    --create-resolver-endpoint) CREATE_RESOLVER_ENDPOINT=true; CLOSED_OPTION_GIVEN="$1"; shift ;;
    --remove-resolver-endpoint) REMOVE_RESOLVER_ENDPOINT=true; CLOSED_OPTION_GIVEN="$1"; shift ;;
    --resolver-allowed-cidrs)
      [[ $# -ge 2 ]] || { log_error "--resolver-allowed-cidrs には社内 DNS の CIDR（カンマ区切り）を指定してください"; exit 1; }
      RESOLVER_ALLOWED_CIDRS="$2"; CLOSED_OPTION_GIVEN="$1"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "不明なオプション: $1"; usage; exit 1 ;;
  esac
done

if [[ "${IMAGE_TAG_OVERRIDE}" == "${UNSET_IMAGE_TAG}" ]]; then
  log_error "イメージのタグが ${UNSET_IMAGE_TAG} です。実在するタグを指定してください"
  exit 1
fi

# Validate the inference scope before touching AWS or CDK.
if [[ "${INFERENCE_SCOPE_GIVEN}" == "true" && "${INFERENCE_SCOPE}" != "global" && "${INFERENCE_SCOPE}" != "jp" ]]; then
  log_error "--inference-scope には global か jp を指定してください（受け取った値: '${INFERENCE_SCOPE}'）"
  exit 1
fi
# Validate the endpoint AZ count before touching AWS or CDK as well.
# CDK synth validates the Resolver CIDR format and the create/CIDR combination.
if [[ "${ENDPOINT_AZ_COUNT_GIVEN}" == "true" && "${ENDPOINT_AZ_COUNT}" != "1" && "${ENDPOINT_AZ_COUNT}" != "2" ]]; then
  log_error "--endpoint-az-count には 1 か 2 を指定してください（受け取った値: '${ENDPOINT_AZ_COUNT}'）"
  exit 1
fi

if [[ "${CREATE_RESOLVER_ENDPOINT}" == "true" && "${REMOVE_RESOLVER_ENDPOINT}" == "true" ]]; then
  log_error "--create-resolver-endpoint と --remove-resolver-endpoint は一緒に使えません"
  exit 1
fi

# Validate closed-network values before touching AWS; public usage is unchanged.
if [[ "${CLOSED}" == "false" && -n "${CLOSED_OPTION_GIVEN}" ]]; then
  log_error "${CLOSED_OPTION_GIVEN} は --closed と一緒に使ってください"
  usage
  exit 1
fi
if [[ "${CLOSED}" == "true" ]]; then
  STAGE="${CLOSED_STAGE}"
  if [[ "${RESTART_APP}" == "false" ]]; then
    missing=()
    [[ -n "${CERTIFICATE_ARN}" ]] || missing+=("--certificate-arn")
    [[ -n "${DOMAIN_NAME}" ]] || missing+=("--domain-name")
    if [[ ${#missing[@]} -gt 0 ]]; then
      log_error "閉域構成には ${missing[*]} が要ります"
      usage
      exit 1
    fi
  fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}"
if [[ ! -f "${PROJECT_ROOT}/cdk/bin/app.ts" ]]; then
  log_error "CDK プロジェクトが見つかりません。プロジェクトのルートで実行してください"
  exit 1
fi
cd "${PROJECT_ROOT}"

# ===== Step 0: working tree and image tag =====
log_step "Step 0: 作業ツリーとイメージのタグ"

image_affecting_changes() {
  # List uncommitted changes to files that go into the image (git status --porcelain format).
  # Paths excluded by .dockerignore (cdk/parameters.ts, docs/ etc.) do not change the image, so skip them.
  # Dockerfile and .dockerignore are not in the image but alter the build, so always check them.
  # If .dockerignore is missing or has "!" re-include patterns, do not interpret it; check the whole tree.
  local pathspecs=(.) line
  if [[ ! -f .dockerignore ]] || grep -qE '^[[:space:]]*!' .dockerignore; then
    git status --porcelain --untracked-files=all
    return
  fi
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    line="${line#/}"
    line="${line%/}"
    [[ -z "${line}" || "${line}" == "Dockerfile" || "${line}" == ".dockerignore" ]] && continue
    pathspecs+=(":(exclude,glob)${line}" ":(exclude,glob)${line}/**")
  done < .dockerignore
  git status --porcelain --untracked-files=all -- "${pathspecs[@]}"
}

# Building with uncommitted image files would put different contents under the same tag.
if [[ "${RESTART_APP}" == "false" ]]; then
  IMAGE_CHANGES=$(image_affecting_changes)
  if [[ -n "${IMAGE_CHANGES}" ]]; then
    log_error "作業ツリーに、コンテナイメージに入るファイルの未コミットの変更があります"
    log_error "イメージのタグにコミットのハッシュを使うため、コミットするか退避してから実行してください"
    log_error "（cdk/parameters.ts など、イメージに入らないファイルの変更はコミットしなくて構いません）"
    echo "${IMAGE_CHANGES}"
    exit 1
  fi
fi

IMAGE_TAG="${IMAGE_TAG_OVERRIDE:-$(git rev-parse --short HEAD)}"
log_info "イメージのタグ: ${IMAGE_TAG}"

stack_output() {
  # stack_output <stack name> <output name>
  aws cloudformation describe-stacks \
    --stack-name "$1" --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" \
    --output text
}

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
log_info "AWS アカウント: ${ACCOUNT_ID}"
log_info "Stage: ${STAGE}"
if [[ "${INFERENCE_SCOPE_GIVEN}" == "true" ]]; then
  log_info "推論の範囲: ${INFERENCE_SCOPE}"
else
  log_info "推論の範囲: 指定なし（parameters.ts の値か global）"
fi
if [[ "${CLOSED}" == "true" ]]; then
  log_info "リージョン: ${REGION}（閉域構成のため Edge なし）"
else
  log_info "リージョン: ${REGION}（Edge: ${EDGE_REGION}）"
fi

# ===== --restart-app: only replace the tasks =====
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

# ===== Closed network: do not shrink a deployed configuration by forgetting arguments =====
# The Resolver and endpoint AZ count are passed only as arguments (context) and are stored nowhere.
# A redeploy that omits them would delete the Resolver (losing the internal DNS forwarding IPs) or
# squeeze endpoints and tasks into one AZ, so compare with the deployed App and stop before changing AWS.
closed_app_resource_ids() {
  # closed_app_resource_ids <resource type>
  aws cloudformation describe-stack-resources --stack-name "${STAGE}-App" --region "${REGION}" \
    --query "StackResources[?ResourceType=='$1'].PhysicalResourceId" --output text
}

if [[ "${CLOSED}" == "true" ]]; then
  if ! stack_error=$(aws cloudformation describe-stacks --stack-name "${STAGE}-App" \
      --region "${REGION}" 2>&1 > /dev/null); then
    if [[ "${stack_error}" != *"does not exist"* ]]; then
      log_error "デプロイ済みの ${STAGE}-App を確かめられません: ${stack_error}"
      exit 1
    fi
    log_info "${STAGE}-App はまだありません（初回のデプロイ）"
  else
    deployed_resolvers=$(closed_app_resource_ids "AWS::Route53Resolver::ResolverEndpoint")
    if [[ -n "${deployed_resolvers}" && "${deployed_resolvers}" != "None" \
        && "${CREATE_RESOLVER_ENDPOINT}" == "false" && "${REMOVE_RESOLVER_ENDPOINT}" == "false" ]]; then
      log_error "デプロイ済みの Resolver Inbound Endpoint（${deployed_resolvers}）が、この引数では消えます"
      log_error "残すなら --create-resolver-endpoint --resolver-allowed-cidrs <前と同じ CIDR> を付け、"
      log_error "消すなら --remove-resolver-endpoint を付けてください（社内 DNS の転送先の IP も失われます）"
      exit 1
    fi
    deployed_az_count="None"
    endpoint_ids=$(closed_app_resource_ids "AWS::EC2::VPCEndpoint")
    if [[ -n "${endpoint_ids}" && "${endpoint_ids}" != "None" ]]; then
      # shellcheck disable=SC2086  # split tab-separated IDs into separate arguments
      deployed_az_count=$(aws ec2 describe-vpc-endpoints --region "${REGION}" \
        --vpc-endpoint-ids ${endpoint_ids} \
        --query "max(VpcEndpoints[?VpcEndpointType=='Interface'].length(SubnetIds))" --output text)
    fi
    if [[ "${deployed_az_count}" == "2" && "${ENDPOINT_AZ_COUNT_GIVEN}" == "false" ]]; then
      log_error "デプロイ済みのエンドポイントは 2 AZ ですが、--endpoint-az-count がありません"
      log_error "（省略すると parameters.ts の値になり、dev は 1 AZ に減ってタスクも 1 AZ に寄ります）"
      log_error "--endpoint-az-count 2 で残すか、--endpoint-az-count 1 で減らすかを明示してください"
      exit 1
    fi
    log_info "デプロイ済みの ${STAGE}-App: Resolver ${deployed_resolvers:-なし}、エンドポイントの AZ の数 ${deployed_az_count}"
  fi
fi

# ===== Step 1: CDK setup =====
log_step "Step 1: CDK の依存のインストールと bootstrap"
cd "${PROJECT_ROOT}/cdk"
npm ci
BOOTSTRAP_REGIONS=("${REGION}")
if [[ "${CLOSED}" == "false" ]]; then
  BOOTSTRAP_REGIONS+=("${EDGE_REGION}")
fi
for target_region in "${BOOTSTRAP_REGIONS[@]}"; do
  npx cdk bootstrap "aws://${ACCOUNT_ID}/${target_region}" --region "${target_region}"
done

CDK_CONTEXT=(-c "imageTag=${IMAGE_TAG}" -c "region=${REGION}")
# Inference scope: passed only when given (otherwise the parameters.ts value, else global).
# The closed synth also builds the public Stage, so the closed value uses a separate name and affects only the target Stage.
if [[ "${INFERENCE_SCOPE_GIVEN}" == "true" ]]; then
  if [[ "${CLOSED}" == "true" ]]; then
    CDK_CONTEXT+=(-c "closedInferenceScope=${INFERENCE_SCOPE}")
  else
    CDK_CONTEXT+=(-c "inferenceScope=${INFERENCE_SCOPE}")
  fi
fi
if [[ "${CLOSED}" == "true" ]]; then
  # The closed Stage is created only when this context is present (cdk/lib/dev-stage.ts)
  CDK_CONTEXT+=(
    -c "closedCertificateArn=${CERTIFICATE_ARN}"
    -c "closedDomainName=${DOMAIN_NAME}"
    -c "closedAllowedCidrs=${ALLOWED_CIDRS}"
    -c "closedCreateTestEnvironment=${CREATE_TEST_ENVIRONMENT}"
  )
  # Key of the Nemotron Parquet placed in advance (closed network); passed only when given (otherwise the default key)
  if [[ -n "${NEMOTRON_DATASET_KEY}" ]]; then
    CDK_CONTEXT+=(-c "closedNemotronDatasetKey=${NEMOTRON_DATASET_KEY}")
  fi
  # Passed only when given (otherwise the parameters.ts values: AZ count 1, no Resolver)
  if [[ "${ENDPOINT_AZ_COUNT_GIVEN}" == "true" ]]; then
    CDK_CONTEXT+=(-c "closedEndpointAzCount=${ENDPOINT_AZ_COUNT}")
  fi
  if [[ "${CREATE_RESOLVER_ENDPOINT}" == "true" ]]; then
    CDK_CONTEXT+=(-c "closedCreateResolverEndpoint=true")
  elif [[ "${REMOVE_RESOLVER_ENDPOINT}" == "true" ]]; then
    # Remove it even if parameters.ts is set to create it
    CDK_CONTEXT+=(-c "closedCreateResolverEndpoint=false")
  fi
  if [[ -n "${RESOLVER_ALLOWED_CIDRS}" ]]; then
    CDK_CONTEXT+=(-c "closedResolverAllowedCidrs=${RESOLVER_ALLOWED_CIDRS}")
  fi
fi

# ===== Step 2: 1. Data (and Edge for public) =====
if [[ "${CLOSED}" == "true" ]]; then
  log_step "Step 2: Data スタック（① テーブル・ECR・Cognito）"
  npx cdk deploy "${STAGE}/Data" --require-approval never "${CDK_CONTEXT[@]}"
else
  log_step "Step 2: Data・Edge スタック（① テーブル・ECR・Cognito・WAF）"
  npx cdk deploy "${STAGE}/Data" "${STAGE}/Edge" --require-approval never "${CDK_CONTEXT[@]}"
fi

ECR_URI=$(stack_output "${STAGE}-Data" EcrRepositoryUri)
ECR_REPOSITORY="${ECR_URI#*/}"
log_info "ECR: ${ECR_URI}"

image_exists() {
  aws ecr describe-images --repository-name "${ECR_REPOSITORY}" \
    --image-ids "imageTag=${IMAGE_TAG}" --region "${REGION}" > /dev/null 2>&1
}

# ===== Step 3: 2. Image =====
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
if [[ "${CLOSED}" == "true" ]]; then
  log_step "Step 4: App スタック（③ Fargate・内部 ALB（HTTPS）・VPC エンドポイント）"
else
  log_step "Step 4: App スタック（③ Fargate・内部 ALB・CloudFront）"
fi
if [[ "${IMAGE_TAG}" == "${UNSET_IMAGE_TAG}" ]]; then
  log_error "イメージのタグが ${UNSET_IMAGE_TAG} のため App スタックをデプロイしません"
  exit 1
fi
cd "${PROJECT_ROOT}/cdk"
npx cdk deploy "${STAGE}/App" --require-approval never "${CDK_CONTEXT[@]}"

# ===== Done =====
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
if [[ "${CLOSED}" == "true" ]]; then
  # The closed configuration uses the app login screen (admin_api); create with a temporary password and change it at first login
  echo "  aws cognito-idp admin-create-user --region ${REGION} \\"
  echo "    --user-pool-id ${USER_POOL_ID} --username <メールアドレス> \\"
  echo "    --user-attributes Name=email,Value=<メールアドレス> Name=email_verified,Value=true \\"
  echo "    --temporary-password '<一時パスワード>' --message-action SUPPRESS"
  echo "  aws cognito-idp admin-add-user-to-group --region ${REGION} \\"
  echo "    --user-pool-id ${USER_POOL_ID} --username <メールアドレス> --group-name role:admin"
  echo ""
  echo "  （一時パスワードは別の経路で伝えます。初回ログインで新しいパスワードを設定します）"
  echo ""
  echo "  アプリは閉域の VPC の中からだけ開けます（${APP_URL}）。"
  if [[ "${CREATE_TEST_ENVIRONMENT}" == "true" ]]; then
    echo "  検証用 EC2 には Systems Manager の Fleet Manager（リモートデスクトップ）で入り、"
    echo "  その中のブラウザで ${APP_URL}/health を開きます（手順は docs/closed_network.md）。"
  fi
  echo ""
  CLOSED_ARGS="--closed --certificate-arn ${CERTIFICATE_ARN} --domain-name ${DOMAIN_NAME}"
  [[ -z "${ALLOWED_CIDRS}" ]] || CLOSED_ARGS+=" --allowed-cidrs ${ALLOWED_CIDRS}"
  [[ "${CREATE_TEST_ENVIRONMENT}" == "false" ]] || CLOSED_ARGS+=" --create-test-environment"
  [[ -z "${NEMOTRON_DATASET_KEY}" ]] || CLOSED_ARGS+=" --nemotron-dataset-key ${NEMOTRON_DATASET_KEY}"
  [[ "${INFERENCE_SCOPE_GIVEN}" == "false" ]] || CLOSED_ARGS+=" --inference-scope ${INFERENCE_SCOPE}"
  [[ "${ENDPOINT_AZ_COUNT_GIVEN}" == "false" ]] || CLOSED_ARGS+=" --endpoint-az-count ${ENDPOINT_AZ_COUNT}"
  [[ "${CREATE_RESOLVER_ENDPOINT}" == "false" ]] || CLOSED_ARGS+=" --create-resolver-endpoint"
  [[ -z "${RESOLVER_ALLOWED_CIDRS}" ]] || CLOSED_ARGS+=" --resolver-allowed-cidrs ${RESOLVER_ALLOWED_CIDRS}"
  echo "再デプロイ（コードを更新したとき）: ./deploy.sh ${CLOSED_ARGS}"
  echo "前の版に戻す:                       ./deploy.sh ${CLOSED_ARGS} --image-tag <前のタグ>"
  echo "秘密を書き換えたあと:               ./deploy.sh --closed --restart-app"
else
  echo "  aws cognito-idp admin-create-user --region ${REGION} \\"
  echo "    --user-pool-id ${USER_POOL_ID} --username <メールアドレス> \\"
  echo "    --user-attributes Name=email,Value=<メールアドレス> Name=email_verified,Value=true"
  echo "  aws cognito-idp admin-add-user-to-group --region ${REGION} \\"
  echo "    --user-pool-id ${USER_POOL_ID} --username <メールアドレス> --group-name role:admin"
  echo ""
  echo "  （仮のパスワードがメールで届きます。初回ログインで新しいパスワードを設定します）"
  echo ""
  PUBLIC_ARGS=""
  [[ "${INFERENCE_SCOPE_GIVEN}" == "false" ]] || PUBLIC_ARGS=" --inference-scope ${INFERENCE_SCOPE}"
  echo "再デプロイ（コードを更新したとき）: ./deploy.sh${PUBLIC_ARGS}"
  echo "前の版に戻す:                       ./deploy.sh${PUBLIC_ARGS} --image-tag <前のタグ>"
  echo "秘密を書き換えたあと:               ./deploy.sh --restart-app"
fi
