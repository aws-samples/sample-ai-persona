import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { acknowledgeAll, NAG_REASONS } from '../nag-reasons';

export interface VpcProps {
  envName: string;
}

/**
 * App VPC (v1.0.0, App stack).
 *
 * 2 AZs, 1 NAT, tasks and the internal ALB in private subnets. DynamoDB and S3 are reached through
 * gateway VPC endpoints without passing the NAT.
 * The VPC holds no state, so it can be recreated with the App stack. The explicit removal
 * (applyRemovalPolicy) that v0 had was dropped (DESTROY is forbidden in this project).
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
    acknowledgeAll(this.vpc, ['VPC7'], NAG_REASONS.accessLogs);
  }
}
