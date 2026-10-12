import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';

export interface VpcProps {
  envName: string;
}

/**
 * アプリの VPC（v1.0.0、App スタック）。
 *
 * 2 AZ、NAT 1 台、タスクと内部 ALB はプライベートサブネット。DynamoDB と S3 は
 * ゲートウェイ型の VPC エンドポイントで NAT を通さずに届く（infrastructure-specification.md）。
 * VPC は状態を持たないため App スタックと一緒に作り直せる。v0 にあった削除の
 * 明示（applyRemovalPolicy）はやめた（project.md の Forbidden）。
 */
export class Vpc extends Construct {
  public readonly vpc: ec2.Vpc;

  constructor(scope: Construct, id: string, props: VpcProps) {
    super(scope, id);

    this.vpc = new ec2.Vpc(this, 'Vpc', {
      vpcName: `ai-persona-v1-${props.envName}`,
      maxAzs: 2,
      natGateways: 1,
      subnetConfiguration: [
        { name: 'Public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: 'Private', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
      ],
      gatewayEndpoints: {
        DynamoDb: { service: ec2.GatewayVpcEndpointAwsService.DYNAMODB },
        S3: { service: ec2.GatewayVpcEndpointAwsService.S3 },
      },
    });
  }
}
