import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import { acknowledgeAll, acknowledgeCfnValidate, NAG_REASONS } from '../nag-reasons';

/**
 * Windows のライセンス認証（KMS）の宛先。EC2 の Windows は 169.254.169.250・251 の TCP 1688 で
 * 認証する（閉域でもリンクローカルのため届く。NFR3.5）。
 */
const WINDOWS_KMS_ADDRESSES = ['169.254.169.250/32', '169.254.169.251/32'];
const WINDOWS_KMS_PORT = 1688;

export interface ValidationEnvironmentProps {
  vpc: ec2.IVpc;
}

/**
 * 閉域の検証用 Windows EC2（U1 閉域、BR6.1・BR6.2、NFR1.4・NFR3.5）。
 *
 * closedCreateTestEnvironment=true のときだけ App スタックが作る。利用者は Fleet Manager
 * （SSM のエンドポイント経由）の RDP で入り、中のブラウザで https://<FQDN> を開く。
 *
 * - Windows Server 2022（日本語）、t3.medium、PRIVATE_ISOLATED、パブリック IP なし、IMDSv2
 * - キーペアは ec2.KeyPair（秘密鍵は SSM パラメータ /ec2/keypair/<ID> に置かれ、Fleet Manager の
 *   RDP の認証に使う）
 * - ロールは AmazonSSMManagedInstanceCore だけ（インラインの権限は足さない）
 * - SG は受け入れなし。外向きはここで KMS の 1688 だけを足し、ALB とエンドポイントへの 443 は
 *   App スタックが結線で足す（AppStack.wireClosedSecurityGroups）
 * - ルートボリュームは暗号化する
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

    // Windows の管理者のパスワードの復号に使う（Fleet Manager の RDP の認証）
    this.keyPair = new ec2.KeyPair(this, 'KeyPair', { type: ec2.KeyPairType.RSA });

    this.instance = new ec2.Instance(this, 'Instance', {
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
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

    // cdk-nag の抑止（U1 閉域、security-design.md §7）。理由は nag-reasons.ts
    acknowledgeAll(this.instance, ['EC28', 'EC29'], NAG_REASONS.validationInstance);
    // サブネットの AZ を固定しているため、インスタンスにも AZ が出る（closed-network.ts）
    acknowledgeCfnValidate(this.instance, ['W3010'], NAG_REASONS.pinnedAvailabilityZones);
    acknowledgeAll(
      this.role,
      ['IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonSSMManagedInstanceCore]'],
      NAG_REASONS.ssmManagedInstance,
    );
  }
}
