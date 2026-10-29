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
#   --inference-scope global|jp
#                      推論の範囲。デプロイする Stage だけに効く（公開は -c inferenceScope=<値>、
#                      --closed は -c closedInferenceScope=<値>）。省略時は parameters.ts の
#                      inferenceScope か global。jp を続けて使うなら parameters.ts に書く（cdk/README.md）
#
# 閉域構成（Stage AIPersonaDevClosed。Data・App の 2 スタック。公開の Stage には触れない）:
#   ./deploy.sh --closed --certificate-arn <ACM の証明書の ARN> --domain-name <FQDN> \
#     [--allowed-cidrs <CIDR,CIDR>] [--create-test-environment] [--endpoint-az-count <1|2>] \
#     [--create-resolver-endpoint --resolver-allowed-cidrs <社内 DNS の CIDR,CIDR>] \
#     [--remove-resolver-endpoint]
#   デプロイ済みの App に Resolver があるのに --create-resolver-endpoint がないとき、
#   エンドポイントが 2 AZ なのに --endpoint-az-count がないときは、AWS を変える前に止まる。
#   ① cdk deploy AIPersonaDevClosed/Data（us-east-1 の bootstrap と Edge はしない）
#   ② イメージを閉域の Data の ECR へ送る ③ cdk deploy AIPersonaDevClosed/App -c imageTag=<タグ>
#   --image-tag・--restart-app・--region も --closed と一緒に使える（対象は閉域の Stage）。
#   --certificate-arn と --domain-name がなければ、AWS に触れる前に使い方を出して終わる。
#
# 事前に: ローカルの検査（pytest・ruff・mypy・CDK の検査）を通し、
#   cd cdk && npx cdk diff 'AIPersonaDev/*' -c imageTag=<タグ>
# でステートフルなリソースの置き換え（replace）がないことを確かめる。
#
# v0 のスタック（AIPersona-dev など）には触れない。v0 を使い続ける場合の手順は README を参照。
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

# 推論の範囲の検査も AWS・CDK に触れる前に行う（閉域対応 U5 BR1.9）
if [[ "${INFERENCE_SCOPE_GIVEN}" == "true" && "${INFERENCE_SCOPE}" != "global" && "${INFERENCE_SCOPE}" != "jp" ]]; then
  log_error "--inference-scope には global か jp を指定してください（受け取った値: '${INFERENCE_SCOPE}'）"
  exit 1
fi
# エンドポイントの AZ の数の検査も同じく AWS・CDK に触れる前に行う（閉域対応 U2 BR5.1）。
# Resolver の CIDR の形と組み合わせ（作る指定と CIDR）は CDK の synth が検査する（BR3.2、BR3.3）
if [[ "${ENDPOINT_AZ_COUNT_GIVEN}" == "true" && "${ENDPOINT_AZ_COUNT}" != "1" && "${ENDPOINT_AZ_COUNT}" != "2" ]]; then
  log_error "--endpoint-az-count には 1 か 2 を指定してください（受け取った値: '${ENDPOINT_AZ_COUNT}'）"
  exit 1
fi

if [[ "${CREATE_RESOLVER_ENDPOINT}" == "true" && "${REMOVE_RESOLVER_ENDPOINT}" == "true" ]]; then
  log_error "--create-resolver-endpoint と --remove-resolver-endpoint は一緒に使えません"
  exit 1
fi

# 閉域の値の検査は AWS に触れる前に行う（BR7.2）。公開の使い方は変えない（BR7.3）
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

# ===== 閉域: デプロイ済みの構成を引数の付け忘れで減らさない =====
# Resolver とエンドポイントの AZ の数は引数（コンテキスト）でしか渡らず、どこにも残らない。
# 付け忘れた再デプロイで Resolver が消える（社内 DNS の転送先の IP が失われる）ことや、
# エンドポイントとタスクが 1 AZ に寄ることを、デプロイ済みの App と比べて AWS を変える前に止める
closed_app_resource_ids() {
  # closed_app_resource_ids <リソースの型>
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
      # shellcheck disable=SC2086  # タブ区切りの ID を 1 つずつの引数にする
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

# ===== Step 1: CDK の準備 =====
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
# 推論の範囲（U5 BR1.9）。渡したときだけ（省略時は parameters.ts の値か global）。
# 閉域の synth は公開の Stage も組み立てるため、閉域は別の名前で渡し、対象の Stage だけに効かせる
if [[ "${INFERENCE_SCOPE_GIVEN}" == "true" ]]; then
  if [[ "${CLOSED}" == "true" ]]; then
    CDK_CONTEXT+=(-c "closedInferenceScope=${INFERENCE_SCOPE}")
  else
    CDK_CONTEXT+=(-c "inferenceScope=${INFERENCE_SCOPE}")
  fi
fi
if [[ "${CLOSED}" == "true" ]]; then
  # 閉域の Stage は、このコンテキストがあるときだけ作られる（cdk/lib/dev-stage.ts）
  CDK_CONTEXT+=(
    -c "closedCertificateArn=${CERTIFICATE_ARN}"
    -c "closedDomainName=${DOMAIN_NAME}"
    -c "closedAllowedCidrs=${ALLOWED_CIDRS}"
    -c "closedCreateTestEnvironment=${CREATE_TEST_ENVIRONMENT}"
  )
  # 事前に配置する Nemotron の Parquet のキー（U3 閉域）。渡したときだけ（省略時は既定のキー）
  if [[ -n "${NEMOTRON_DATASET_KEY}" ]]; then
    CDK_CONTEXT+=(-c "closedNemotronDatasetKey=${NEMOTRON_DATASET_KEY}")
  fi
  # 閉域対応 U2（BR5.1）。渡したときだけ（省略時は parameters.ts の値: AZ の数 1、Resolver なし）
  if [[ "${ENDPOINT_AZ_COUNT_GIVEN}" == "true" ]]; then
    CDK_CONTEXT+=(-c "closedEndpointAzCount=${ENDPOINT_AZ_COUNT}")
  fi
  if [[ "${CREATE_RESOLVER_ENDPOINT}" == "true" ]]; then
    CDK_CONTEXT+=(-c "closedCreateResolverEndpoint=true")
  elif [[ "${REMOVE_RESOLVER_ENDPOINT}" == "true" ]]; then
    # parameters.ts で作る設定にしていても消す
    CDK_CONTEXT+=(-c "closedCreateResolverEndpoint=false")
  fi
  if [[ -n "${RESOLVER_ALLOWED_CIDRS}" ]]; then
    CDK_CONTEXT+=(-c "closedResolverAllowedCidrs=${RESOLVER_ALLOWED_CIDRS}")
  fi
fi

# ===== Step 2: ① Data（公開は Edge も） =====
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
if [[ "${CLOSED}" == "true" ]]; then
  # 閉域構成はアプリのログイン画面（admin_api）を使う。一時パスワードで作り、初回ログインで変える
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
