import { flattenPolicyValue, patternCovers, statementsAllow } from './helpers/policy-coverage';

/**
 * 包含の判定（テスト用の補助、閉域対応 U2 BR1.4）の単体テスト。
 *
 * test/closed-stage.test.ts が、タスクロールと実行ロールの文の Resource がエンドポイントの
 * ポリシーに含まれることを、この判定で確かめる。IAM と同じワイルドカードの解釈（`*` はスラッシュを
 * 含む任意の文字列、`?` は 1 文字）で、CloudFormation の参照は同じ式どうしを同じ値として比べる。
 */

const TABLE = 'arn:aws:dynamodb:ap-northeast-1:123456789012:table/Personas';

describe('patternCovers: IAM と同じワイルドカード', () => {
  test('* はスラッシュとコロンを含む任意の文字列（空も含む）に一致する', () => {
    expect(patternCovers('arn:aws:s3:::bucket/*', 'arn:aws:s3:::bucket/uploads/a/b.png')).toBe(true);
    expect(patternCovers('arn:aws:logs:r:a:log-group:app:*', 'arn:aws:logs:r:a:log-group:app:log-stream:x')).toBe(true);
    expect(patternCovers('arn:aws:bedrock:*::foundation-model/*', 'arn:aws:bedrock:::foundation-model/m')).toBe(true);
    expect(patternCovers('*', 'anything')).toBe(true);
  });

  test('? はちょうど 1 文字に一致する', () => {
    expect(patternCovers('a?c', 'abc')).toBe(true);
    expect(patternCovers('a?c', 'abbc')).toBe(false);
    expect(patternCovers('a?c', 'ac')).toBe(false);
  });

  test('名前付きの GSI の ARN は <テーブル>/index/* に含まれ、テーブルの ARN だけには含まれない', () => {
    expect(patternCovers(`${TABLE}/index/*`, `${TABLE}/index/ByOwner`)).toBe(true);
    expect(patternCovers(TABLE, `${TABLE}/index/ByOwner`)).toBe(false);
    expect(patternCovers(`${TABLE}/index/*`, TABLE)).toBe(false);
  });

  test('ロール側のワイルドカードは、エンドポイント側のより広いワイルドカードにだけ含まれる', () => {
    expect(patternCovers('arn:aws:s3:::bucket/*', 'arn:aws:s3:::bucket/uploads/*')).toBe(true);
    expect(patternCovers('arn:aws:s3:::bucket/uploads/*', 'arn:aws:s3:::bucket/*')).toBe(false);
    expect(patternCovers('a?c', 'a*c')).toBe(false);
    expect(patternCovers('a*c', 'a?c')).toBe(true);
    expect(patternCovers('abc', 'a?c')).toBe(false);
  });

  test('含まれない例: 別のバケット、別のアカウント', () => {
    expect(patternCovers('arn:aws:s3:::bucket/*', 'arn:aws:s3:::other/x')).toBe(false);
    expect(
      patternCovers(
        'arn:aws:bedrock:*:123456789012:inference-profile/*',
        'arn:aws:bedrock:us-east-1:999999999999:inference-profile/x',
      ),
    ).toBe(false);
  });
});

describe('flattenPolicyValue: CloudFormation の参照どうしの一致', () => {
  const importedBucket = (suffix: string) => ({ 'Fn::Join': ['', [{ 'Fn::ImportValue': 'Data:BucketArn' }, suffix]] });

  test('同じ式は同じ文字列になり、包含も判定できる', () => {
    expect(flattenPolicyValue(importedBucket('/*'))).toBe(flattenPolicyValue(importedBucket('/*')));
    expect(patternCovers(flattenPolicyValue(importedBucket('/*')), flattenPolicyValue(importedBucket('/uploads/*')))).toBe(true);
    expect(flattenPolicyValue({ 'Fn::GetAtt': ['AppLogGroup', 'Arn'] })).toBe(
      flattenPolicyValue({ 'Fn::GetAtt': ['AppLogGroup', 'Arn'] }),
    );
  });

  test('別の出力名・別のリソースの参照は一致しない', () => {
    const other = { 'Fn::Join': ['', [{ 'Fn::ImportValue': 'Data:OtherArn' }, '/uploads/*']] };
    expect(patternCovers(flattenPolicyValue(importedBucket('/*')), flattenPolicyValue(other))).toBe(false);
    expect(
      patternCovers(flattenPolicyValue({ Ref: 'SecretA' }), flattenPolicyValue({ Ref: 'SecretB' })),
    ).toBe(false);
    expect(
      patternCovers(
        flattenPolicyValue({ 'Fn::GetAtt': ['TableA', 'Arn'] }),
        flattenPolicyValue({ 'Fn::GetAtt': ['TableB', 'Arn'] }),
      ),
    ).toBe(false);
  });

  test('参照の中の文字は、エンドポイント側のワイルドカードの一部として扱わない', () => {
    // 参照は 1 つのまとまり。'*' だけの Resource はどの参照も含む
    expect(patternCovers('*', flattenPolicyValue({ Ref: 'SecretA' }))).toBe(true);
    expect(patternCovers('Secret?', flattenPolicyValue({ Ref: 'SecretA' }))).toBe(false);
  });

  test('疑似パラメータ（AWS::Partition）を含む式も同じ式どうしで一致する', () => {
    const layer = { 'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' }, ':s3:::prod-ap-northeast-1-starport-layer-bucket/*']] };
    expect(patternCovers(flattenPolicyValue(layer), flattenPolicyValue(layer))).toBe(true);
  });
});

describe('statementsAllow: Action と Resource の両方で判定する', () => {
  const statements = [
    { Effect: 'Allow', Principal: '*', Action: 's3:*', Resource: ['arn:aws:s3:::bucket', 'arn:aws:s3:::bucket/*'] },
    { Effect: 'Allow', Principal: '*', Action: 's3:GetObject', Resource: 'arn:aws:s3:::layer/*' },
    { Effect: 'Deny', Principal: '*', Action: 's3:*', Resource: 'arn:aws:s3:::denied/*' },
  ];

  test('サービスの * は、そのサービスの操作（大文字・小文字を区別しない）を含む', () => {
    expect(statementsAllow(statements, 's3:PutObject', 'arn:aws:s3:::bucket/uploads/x')).toBe(true);
    expect(statementsAllow(statements, 'S3:putobject', 'arn:aws:s3:::bucket/uploads/x')).toBe(true);
  });

  test('名前の操作の文は、ほかの操作を含まない', () => {
    expect(statementsAllow(statements, 's3:GetObject', 'arn:aws:s3:::layer/a')).toBe(true);
    expect(statementsAllow(statements, 's3:PutObject', 'arn:aws:s3:::layer/a')).toBe(false);
  });

  test('Deny の文と、別のサービスの操作は許可にならない', () => {
    expect(statementsAllow(statements, 's3:GetObject', 'arn:aws:s3:::denied/a')).toBe(false);
    expect(statementsAllow(statements, 'dynamodb:GetItem', 'arn:aws:s3:::bucket/x')).toBe(false);
  });
});
