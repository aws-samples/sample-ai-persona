import { Construct } from 'constructs';
import { RemovalPolicy } from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';

/**
 * Home of the app container image (v1.0.0, Data stack).
 *
 * - Tags are immutable (IMMUTABLE). deploy.sh tags with the short Git commit hash
 * - Keeps the latest 20 images so there is something to roll back to
 * - Lives in the Data stack because it is needed before the App stack; RETAIN
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
