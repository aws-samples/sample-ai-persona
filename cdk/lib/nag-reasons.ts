import { IConstruct } from 'constructs';
import { CfnResource, Stack, Validations } from 'aws-cdk-lib';

/**
 * cdk-nag（AwsSolutions）の抑止の理由（U9、NFR7.15、Infrastructure Design Q1〜Q4）。
 *
 * 同じ理由を何か所にも書かないよう、理由の文はここにだけ置く。抑止はリソース単位で、
 * 指摘の出る construct のそばで acknowledgeAll() を呼ぶ。スタックやアプリには付けない。
 * 抑止を足す・消す変更は、同じ変更で test/cdk-nag.test.ts の期待の一覧を直す。
 */
export const NAG_REASONS = {
  /** ELB2・CFR3・VPC7・S1（NFR Design Q1 C）。 */
  accessLogs:
    'アクセスログ・フローログは作らない: dev で利用者が数人、保管の費用と管理の対象が増え、' +
    'v1.0.0 の要件にない。本番の Stage を足すときに見直す',
  /** COG2（NFR Design Q2 A）。 */
  cognitoMfa:
    'MFA は v1.0.0 の要件にない。外部 IdP を使う構成では IdP の側で MFA をかける',
  /** COG8（cdk-nag 2 の COG3 にあたる。NFR Design Q2 A）。 */
  cognitoPlusPlan:
    'Cognito の Plus プラン（高度なセキュリティ）は使わないと決めた（U8 NFR Requirements Q1 A、費用）',
  /** SMG4: Data スタックの 2 つの鍵（NFR3.14）。 */
  manualKeyRotation:
    '自動ローテーションはしない: 鍵は手で新しい版を足してから切り替える設計で、' +
    '古い版で暗号化・署名した値を読み続けるため（security-design.md「暗号化と鍵」）',
  /** SMG4: App スタックの CognitoClientSecret（NFR3.14）。 */
  cognitoClientSecret:
    '値は Cognito のアプリクライアントが決め、Secrets Manager の側で回せない。' +
    '回すときはアプリクライアントを作り直す',
  /** CFR1（warning、Infrastructure Design Q4 A）。 */
  geoRestriction:
    '国で絞る要件がない。入口はアプリのログイン（Cognito）と WAF で守る',
  /** CFR2（warning）: enableWaf が false の構成だけで出る（利用者の指示、2026-10-17）。 */
  wafOptional:
    'WAF は parameters.ts の enableWaf で利用者が選ぶ（費用）。dev の既定は無効で、' +
    'ログイン系の総当たりは Cognito の試行回数の上限とアプリの回路遮断で守る',
  /** CFR4。 */
  cloudFrontDefaultCertificate:
    '既定の *.cloudfront.net の証明書では最小の TLS を選べない。独自ドメインは v1.0.0 の' +
    '対象外。minimumProtocolVersion は TLS 1.2（TLS_V1_2_2021）にしてある',
  /** ECS2。 */
  ecsEnvironment:
    'コンテナの環境変数は秘密でない設定（テーブル名・リージョン・モデル ID など）だけで、' +
    '秘密は Secrets Manager から ECS のシークレットとして渡す',
  /** ECS4（Infrastructure Design Q3 B）。 */
  containerInsights:
    'Container Insights は有効にしない: dev で費用が増え、ECS の既定のメトリクスで足りる。' +
    '本番の Stage を足すときに見直す',
  /** IAM4: CDK の内部の作るカスタムリソースの Lambda。 */
  cdkCustomResourceManagedPolicy:
    'CDK の AwsCustomResource（アプリクライアントの秘密の取得）の Lambda に CDK が付ける ' +
    'AWSLambdaBasicExecutionRole。アプリのロールには AWS 管理ポリシーを付けない',
  /** IAM5: Resource::* の例外（test/iam-wildcards.test.ts の一覧）。 */
  wildcardListApis:
    'リソースを指定できない API だけを * にしている（一覧の API、ECR の認可トークン、' +
    'Mantle の CallWithBearerToken）。一覧は test/iam-wildcards.test.ts で固定する',
  /** IAM5: アップロード用バケットのオブジェクト。 */
  bucketObjects:
    'Data スタックのアップロード用バケットの中のオブジェクト（全体か、用途ごとの接頭辞）だけを許す',
  /** IAM5: Bedrock の推論の ARN。 */
  bedrockInference:
    '推論で呼ぶモデルはパラメータで変わるため、基盤モデル・このアカウントの推論プロファイルの ' +
    'ARN の種類までに絞る（リージョンはグローバルな推論のため *）',
  /** IAM5: Bedrock のバッチ推論ジョブ・KB。 */
  bedrockAccountResources:
    'ジョブ・ナレッジベースの ID は実行時に決まるため、このアカウント・リージョンの ' +
    'model-invocation-job/*・knowledge-base/* に絞る',
  /** IAM5: AgentCore Memory の子のリソース。 */
  memoryChildren:
    'この Stage の AgentCore Memory の子（イベント・記憶のレコード）を操作するため <Memory の ARN>/* を許す',
  /** IAM5: Bedrock Mantle の project。 */
  mantleProject:
    'Mantle の project はこのアカウントのものに絞る。リージョンはアプリが非対応のリージョンから ' +
    'us-east-1 へ送るため *',
  /** IAM5: grantReadWriteData の KMS の操作。 */
  kmsDataKeyActions:
    'Sessions テーブルのカスタマー管理キーへの grantReadWriteData が付ける kms:GenerateDataKey*・' +
    'kms:ReEncrypt*。対象はそのキーの ARN だけ',
  /** VPC7: 閉域の VPC（U1 閉域、security-design.md §7）。U2 までの一時的な抑止。 */
  closedFlowLogsUntilU2:
    '閉域の VPC の Flow Logs は U2（u2-closed-network-hardening）で作る。U1 の細い一本では' +
    'エンドポイントの結線を確かめるところまでで、それまでの一時的な抑止',
  /**
   * W3010（aws-cdk-lib の CloudFormation Validate の warning）: 閉域の VPC のサブネットと検証用 EC2
   * （U1 閉域、Infrastructure Design レビュー R-05）。
   */
  pinnedAvailabilityZones:
    'Bedrock・AgentCore のインターフェイス型エンドポイントを提供する AZ が限られるため、閉域の VPC の ' +
    'AZ を固定している（closed-network.ts の CLOSED_AVAILABILITY_ZONES）',
  /** EC28・EC29: 検証用 EC2（U1 閉域）。 */
  validationInstance:
    '検証用で既定は作らない（closedCreateTestEnvironment=true のときだけ）。データを持たず、' +
    '使い終えたら止めるか消す。詳細監視と終了保護は付けない',
  /** IAM4: 検証用 EC2 のロール（U1 閉域、NFR1.4）。 */
  ssmManagedInstance:
    'Fleet Manager（SSM）の接続に AWS が用意する最小のポリシー AmazonSSMManagedInstanceCore だけを付け、' +
    'インラインの権限は足さない',
} as const;

/**
 * The IAM5 finding ID for a resource string, rendered the way cdk-nag flattens CloudFormation
 * references (`Ref` becomes `<AWS::Partition>`, `Fn::Join` is joined, `Fn::ImportValue` becomes
 * the export name).
 *
 * Needed because the partition and account are tokens unless the feature flag
 * `@aws-cdk/core:enablePartitionLiterals` and a concrete account are set, and a token
 * cannot be used as an acknowledgement key.
 */
export function iam5ResourceId(scope: IConstruct, resource: string): string {
  const flatten = (node: unknown): string => {
    if (typeof node === 'string') {
      return node;
    }
    const value = node as Record<string, any>;
    if (value && Array.isArray(value['Fn::Join'])) {
      const [delimiter, items] = value['Fn::Join'] as [string, unknown[]];
      return items.map(flatten).join(delimiter);
    }
    if (value && value['Fn::ImportValue'] !== undefined) {
      return flatten(value['Fn::ImportValue']);
    }
    if (value && typeof value.Ref === 'string') {
      return `<${value.Ref}>`;
    }
    return JSON.stringify(node);
  };
  return `IAM5[Resource::${flatten(Stack.of(scope).resolve(resource))}]`;
}

/**
 * The IAM5 finding ID for `<value><suffix>`, where `value` is a resource attribute of
 * `producer` that another stack uses (an automatic cross-stack reference, e.g. a bucket ARN).
 *
 * In the consumer's template that resource is `Fn::ImportValue` of an export whose name CDK
 * generates from the logical ID, and cdk-nag puts that name into the finding ID. The name is
 * not written in the code: `Stack.exportValue()` without a name returns the import of the same
 * export as the automatic reference (documented, and CDK's own reference resolution calls it),
 * and a second call reuses the existing output. No export is added, renamed or removed.
 */
export function iam5ExportedResourceId(producer: Stack, value: string, suffix: string): string {
  return iam5ResourceId(producer, `${producer.exportValue(value)}${suffix}`);
}

/**
 * Acknowledge rules of aws-cdk-lib's CloudFormation Validate plugin on one resource with one
 * reason. `ids` are the rule IDs without the plugin prefix (e.g. `W3010`); the plugin reports
 * them as `CloudFormation-Validate::<id>`.
 */
export function acknowledgeCfnValidate(construct: IConstruct, ids: readonly string[], reason: string): void {
  const target = CfnResource.isCfnResource(construct)
    ? construct
    : (construct.node.defaultChild ?? construct);
  for (const id of ids) {
    Validations.of(target).acknowledge({ id: `CloudFormation-Validate::${id}`, reason });
  }
}

/**
 * Acknowledge cdk-nag rules on one resource with one reason.
 *
 * Pass the CfnResource (or its L2 construct, whose default child is used) so that the
 * suppression stays on that resource only. `ids` are rule IDs without the pack prefix
 * (e.g. `S1`, `IAM5[Resource::*]`).
 *
 * `Validations.acknowledge()` rejects IDs that contain `::` more than once, which some
 * finding IDs do (`IAM5[Resource::arn:aws:bedrock:*::foundation-model/*]`,
 * `IAM4[Policy::arn:<AWS::Partition>:...]`). For those, the same acknowledged-rules
 * metadata entry that `acknowledge()` records is written directly; cdk-nag reads only
 * that metadata.
 */
export function acknowledgeAll(construct: IConstruct, ids: readonly string[], reason: string): void {
  const target = CfnResource.isCfnResource(construct)
    ? construct
    : (construct.node.defaultChild ?? construct);
  for (const id of ids.map((rule) => `AwsSolutions-${rule}`)) {
    if (id.split('::').length > 2) {
      target.node.addMetadata(Validations.ACKNOWLEDGED_RULES_METADATA_KEY, { [id]: reason });
    } else {
      Validations.of(target).acknowledge({ id, reason });
    }
  }
}
