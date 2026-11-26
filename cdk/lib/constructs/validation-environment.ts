import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import { acknowledgeAll, acknowledgeCfnValidate, NAG_REASONS } from '../nag-reasons';

/**
 * Destination for Windows activation (KMS). Windows on EC2 activates over TCP 1688 to
 * 169.254.169.250 and .251 (reachable even in the closed network because it is link-local).
 */
const WINDOWS_KMS_ADDRESSES = ['169.254.169.250/32', '169.254.169.251/32'];
const WINDOWS_KMS_PORT = 1688;

export interface ValidationEnvironmentProps {
  vpc: ec2.IVpc;
  /**
   * Subnet for the instance (default PRIVATE_ISOLATED). With one AZ in the closed
   * configuration, pass the same AZ as the endpoints.
   */
  subnets?: ec2.SubnetSelection;
}

/**
 * Windows EC2 for validating the closed network.
 *
 * The App stack creates it only when closedCreateTestEnvironment=true. Users connect over RDP via
 * Fleet Manager (through the SSM endpoints) and open https://<FQDN> in the browser inside.
 *
 * - Windows Server 2022 (Japanese), t3.medium, PRIVATE_ISOLATED, no public IP, IMDSv2
 * - Key pair is ec2.KeyPair (the private key is stored in the SSM parameter /ec2/keypair/<ID> and used
 *   for Fleet Manager RDP authentication)
 * - Role is AmazonSSMManagedInstanceCore only (no inline permissions)
 * - SG has no ingress. Only KMS 1688 egress is added here; 443 to the ALB and endpoints is added by
 *   the App stack wiring (AppStack.wireClosedSecurityGroups)
 * - Root volume is encrypted
 */
export class ValidationEnvironment extends Construct {
  public readonly instance: ec2.Instance;
  public readonly securityGroup: ec2.SecurityGroup;
  public readonly role: iam.Role;
  public readonly keyPair: ec2.KeyPair;

  constructor(scope: Construct, id: string, props: ValidationEnvironmentProps) {
    super(scope, id);

    const { vpc } = props;

    this.securityGroup = new ec2.SecurityGroup(this, 'SecurityGroup', {
      vpc,
      description: 'Validation Windows instance of the AI Persona closed network (no inbound)',
      allowAllOutbound: false,
    });
    for (const address of WINDOWS_KMS_ADDRESSES) {
      this.securityGroup.addEgressRule(
        ec2.Peer.ipv4(address),
        ec2.Port.tcp(WINDOWS_KMS_PORT),
        'Windows activation (KMS)',
      );
    }

    this.role = new iam.Role(this, 'Role', {
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      description: 'AI Persona closed network validation instance (Fleet Manager only)',
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore')],
    });

    // Used to decrypt the Windows administrator password (Fleet Manager RDP authentication)
    this.keyPair = new ec2.KeyPair(this, 'KeyPair', { type: ec2.KeyPairType.RSA });

    this.instance = new ec2.Instance(this, 'Instance', {
      vpc,
      vpcSubnets: props.subnets ?? { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      instanceType: new ec2.InstanceType('t3.medium'),
      machineImage: ec2.MachineImage.latestWindows(
        ec2.WindowsVersion.WINDOWS_SERVER_2022_JAPANESE_FULL_BASE,
      ),
      securityGroup: this.securityGroup,
      role: this.role,
      keyPair: this.keyPair,
      associatePublicIpAddress: false,
      requireImdsv2: true,
      blockDevices: [
        {
          deviceName: '/dev/sda1',
          volume: ec2.BlockDeviceVolume.ebs(50, {
            encrypted: true,
            volumeType: ec2.EbsDeviceVolumeType.GP3,
          }),
        },
      ],
    });

    // cdk-nag suppressions; reasons are in nag-reasons.ts
    acknowledgeAll(this.instance, ['EC28', 'EC29'], NAG_REASONS.validationInstance);
    // The subnet AZ is pinned, so the instance also reports an AZ (closed-network.ts)
    acknowledgeCfnValidate(this.instance, ['W3010'], NAG_REASONS.pinnedAvailabilityZones);
    acknowledgeAll(
      this.role,
      ['IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonSSMManagedInstanceCore]'],
      NAG_REASONS.ssmManagedInstance,
    );
  }
}
