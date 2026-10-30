# 利用者・ロール・グループの管理

AI ペルソナシステムの利用者は、Amazon Cognito の User Pool で管理します。利用者のセルフサインアップはなく、管理者が AWS マネジメントコンソールか AWS CLI（CloudShell など）で作ります。閉域構成では手順が一部違います。違いは [閉域構成ガイドの「利用者の管理」](closed_network.md#利用者の管理) を参照してください。

## ロールとグループ

利用者の権限は、Cognito のグループで決まります。

| グループ | 意味 | 作り方 |
|---|---|---|
| `role:admin` | 管理者。すべてのデータと設定を扱える | デプロイで作られる |
| `role:member` | 一般の利用者。自分のデータと、共有されたデータを扱える | デプロイで作られる |
| `group:<ID>` | 利用者のまとまり（チームや組織など）。共有と、ナレッジベースなどの利用範囲の単位 | 管理者が作る（下の「[グループを作る](#グループを作る)」） |

- 利用者は `role:admin` か `role:member` のどちらかに入れます。どちらにも入っていない利用者は、ログインすると「利用権限がまだありません」の画面になり、ログアウトしかできません
- グループには、いくつでも入れられます。グループに入っていない member は、自分のデータを共有できません
- admin と member でできることの違いは、[ユーザーガイドの「ロールごとにできること」](user_guide.md#ロールごとにできること) を参照してください
- グループの変更は、本人がログインし直すとすぐに反映されます。ログインし直さなくても、最長 1 時間ほどで反映されます

## 準備

### コンソールで

1. AWS マネジメントコンソールで Amazon Cognito を開く
2. 「ユーザープール」から、AI ペルソナシステムのユーザープールを選ぶ

ユーザープールの名前は自動で付くため、どれか分からないときは User Pool ID で探します。User Pool ID は `deploy.sh` の最後に表示されます。CloudFormation の `AIPersonaDev-Data` スタックの「出力」タブの `UserPoolId` でも確かめられます。

### AWS CLI で

以下のコマンドは、次の 2 つの変数を使います。

```bash
REGION=<デプロイしたリージョン>   # deploy.sh の既定は ap-northeast-1
USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name AIPersonaDev-Data --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
```

## 利用者を作る

仮のパスワードがメールで届き、本人が初回ログインで新しいパスワードを設定します。

### コンソールで

1. ユーザープールの「ユーザー」を開き、「ユーザーを作成」を押す
2. 招待メッセージに「E メールで招待を送信」を選ぶ
3. 「ユーザー名」と「E メールアドレス」にメールアドレスを入れ、「E メールアドレスを検証済みとしてマーク」を選ぶ
4. パスワードは「パスワードを生成」を選ぶ
5. 「ユーザーを作成」を押す

続けて、下の「[ロールを付ける](#ロールを付ける)」で `role:admin` か `role:member` に入れます。

### AWS CLI で

```bash
aws cognito-idp admin-create-user --region "$REGION" --user-pool-id "$USER_POOL_ID" \
  --username <メールアドレス> \
  --user-attributes Name=email,Value=<メールアドレス> Name=email_verified,Value=true
```

## ロールを付ける

ロールを変えるときは、新しいロールに入れてから、前のロールから外します。

### コンソールで

1. ユーザープールの「グループ」を開き、`role:member`（admin にするときは `role:admin`）を選ぶ
2. 「ユーザーをグループに追加」を押し、利用者を選んで「追加」を押す

外すときは、同じグループの画面でメンバーの利用者を選び、「グループから削除」を押します。利用者の画面の「グループメンバーシップ」からも追加・削除できます。

### AWS CLI で

```bash
# 入れる（admin にするときは role:admin）
aws cognito-idp admin-add-user-to-group --region "$REGION" --user-pool-id "$USER_POOL_ID" \
  --username <メールアドレス> --group-name role:member
# 外す
aws cognito-idp admin-remove-user-from-group --region "$REGION" --user-pool-id "$USER_POOL_ID" \
  --username <メールアドレス> --group-name role:member
```

## グループを作る

グループは `group:` で始まる名前の Cognito のグループです。`group:` の後ろは ID（英数字を推奨）にし、画面に出す名前は説明に書きます。説明がないときは、ID がそのまま画面に出ます。グループを作ったら、上の「ロールを付ける」と同じ方法で利用者を入れます。

### コンソールで

1. ユーザープールの「グループ」を開き、「グループを作成」を押す
2. 「グループ名」に `group:sales` のような名前を、「説明」に画面に出す名前（例: 営業部）を入れる。ほかの項目は空のままでかまいません
3. 「作成」を押す

### AWS CLI で

```bash
aws cognito-idp create-group --region "$REGION" --user-pool-id "$USER_POOL_ID" \
  --group-name group:sales --description '営業部'
aws cognito-idp admin-add-user-to-group --region "$REGION" --user-pool-id "$USER_POOL_ID" \
  --username <メールアドレス> --group-name group:sales
```

グループを消すと、そのグループへの共有と利用範囲は効かなくなります。利用範囲の画面には「（削除されたグループ）」と出るので、外して保存してください。

## 一覧を見る

### コンソールで

- グループの一覧（ロールのグループを含む）: ユーザープールの「グループ」
- 利用者が入っているグループ: 「ユーザー」で利用者を選び、「グループメンバーシップ」

### AWS CLI で

```bash
# グループの一覧（ロールのグループを含む）
aws cognito-idp list-groups --region "$REGION" --user-pool-id "$USER_POOL_ID" \
  --query 'Groups[].[GroupName,Description]' --output table
# 利用者が入っているグループ
aws cognito-idp admin-list-groups-for-user --region "$REGION" --user-pool-id "$USER_POOL_ID" \
  --username <メールアドレス> --query 'Groups[].GroupName' --output text
```

## パスワードを忘れたとき

本人がログイン画面の「パスワードを忘れた場合」から、メールで再設定できます。

## 利用を止める

退職などで利用を止めるときは、利用者を無効にします。無効にした利用者はログインできません。利用者が作ったデータ（ペルソナ・議論など）は残ります。

### コンソールで

ユーザープールの「ユーザー」で利用者を選び、「ユーザーアクセスを無効化」を押します。

### AWS CLI で

```bash
aws cognito-idp admin-disable-user --region "$REGION" --user-pool-id "$USER_POOL_ID" \
  --username <メールアドレス>
```
