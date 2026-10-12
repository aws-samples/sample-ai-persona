import { Construct } from 'constructs';
import { RemovalPolicy } from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';

/**
 * アプリのコンテナイメージの置き場（v1.0.0、Data スタック）。
 *
 * - タグは不変（IMMUTABLE）。deploy.sh は Git のコミットの短いハッシュをタグにする
 *   （Infrastructure Design レビュー R-10）
 * - 戻し先を残すため、最新 20 個のイメージを残す
 * - App スタックより先に要るため Data スタックに置き、RETAIN にする
 */
export class EcrRepository extends Construct {
  public readonly repository: ecr.Repository;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    this.repository = new ecr.Repository(this, 'Repository', {
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      imageScanOnPush: true,
      removalPolicy: RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          description: 'Keep the latest 20 images as rollback targets',
          maxImageCount: 20,
        },
      ],
    });
  }
}
