# slack-mcp

複数の Slack アカウント (ログインのメールアドレスごと・ワークスペースごと) を、1 つの MCP サーバーで扱う。
claude.ai の Slack コネクタは 1 つのアカウントしかつなげないので、その代わりに自分のサーバーで動かし、同じようなツールを提供する。

- AI (claude.ai・ChatGPT など) ごとに別のコネクタ URL を発行し、投稿は AI ごとの名前・アイコンの bot として行う
- 読み取り (履歴・検索など) は持ち主本人の権限 (ユーザートークン) で行う
- 管理画面から、AI のコネクタと Slack アカウントを設定できる

> 日本語で使う前提で作っている (ツールの説明・管理画面・エラーは日本語。時刻の既定は日本時間)。

---

## 全体像

```
 claude.ai ─▶ https://example.com/slack_mcp/mcp/<claude のトークン>  ─┐
 ChatGPT   ─▶ https://example.com/slack_mcp/mcp/<chatgpt のトークン> ─┤
                                                                    ▼
                  リバースプロキシ ─▶ mcp_server/server.py (127.0.0.1:8009)
                                       │ clients.json でトークンから AI を決める (表示名・アイコン・投稿方法)
                 accounts/<アカウント>.toml ┤ 読む: user_token (xoxp・持ち主本人)
                                       └ 投稿: bot_token (xoxb) + AI ごとの表示名・アイコン

 ブラウザ ─▶ https://example.com/slack_mcp/admin/ ─▶ リバースプロキシ ─▶ web/api.py (127.0.0.1:8010)
                                                          clients.json と accounts/*.toml を書く
```

| ファイル | 役割 |
| --- | --- |
| `mcp_server/server.py` | MCP サーバー本体 (Streamable HTTP)。ツールの定義と、URL のトークンの確認 |
| `workspace.py` | アカウントごとの状態 (自分のユーザー ID・ユーザー名・チャンネル名のキャッシュ) と、Slack の応答の整形 |
| `slack_client.py` | Slack Web API の薄いクライアント (レート制限の再試行、ファイルの送受信) |
| `accounts.py` | `accounts/<アカウント名>.toml` の読み込み |
| `clients.py` | AI ごとのコネクタ (URL のトークン・表示名・アイコン・投稿方法・使えるアカウント) の管理 |
| `web/api.py` / `web/index.html` | 管理画面 (FastAPI と、それが配る 1 枚の HTML。ビルドは要らない) |
| `mcp_server/slack-mcp.service` / `web/slack-mcp-admin.service` | systemd の定義の例 |
| `slack_app_manifest.json` | Slack アプリを作るときのマニフェスト (必要なスコープが入っている) |
| `account.example.toml` | アカウントの設定ファイルの例 |
| `tests/` | 偽の Slack API に対する通し試験と、管理画面の API の試験 |

実行中に、リポジトリの直下に次のものが作られる。どれも Git 管理外で、権限 600 (ディレクトリは 700) で書かれる。

| ファイル | 内容 |
| --- | --- |
| `accounts/<アカウント名>.toml` | Slack のトークン (アカウントごと) |
| `clients.json` | AI ごとのコネクタの設定 (URL のトークンを含む) |

---

## 必要なもの

- Linux などの常時動いているサーバー (Python 3.10 以上。例では systemd で動かす)
- HTTPS で公開できるリバースプロキシ (Apache・nginx など)。コネクタの URL にトークンを載せるので、**HTTP で公開しない**
- Slack のワークスペースにアプリを入れられること (管理者の承認が要るワークスペースもある)

---

## 導入

以下では、配置先を `/opt/slack-mcp`、公開する URL を `https://example.com/slack_mcp/mcp/` (MCP サーバー) と
`https://example.com/slack_mcp/admin/` (管理画面) として書く。自分の環境に合わせて読み替える。

### 1. 配置

```bash
sudo git clone https://github.com/swirhen/slack-mcp.git /opt/slack-mcp
sudo chown -R <動かすユーザー>: /opt/slack-mcp
cd /opt/slack-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. systemd に登録する

`mcp_server/slack-mcp.service` (MCP サーバー) と `web/slack-mcp-admin.service` (管理画面) は例。
`User`・`Group`・`WorkingDirectory`・`ExecStart` のパスを、自分の環境に合わせて書き換えてから登録する。

管理画面のログイン情報を、root だけが読めるファイルに書く:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/default/slack-mcp-admin
sudoedit /etc/default/slack-mcp-admin
```

```
SLACK_MCP_ADMIN_USERNAME=…
SLACK_MCP_ADMIN_PASSWORD=…
SLACK_MCP_ADMIN_SECRET=…    # Cookie の署名用。長いランダムな文字列 (openssl rand -hex 32)。変えると全員ログアウトする
```

```bash
sudo install -m 0644 mcp_server/slack-mcp.service web/slack-mcp-admin.service /etc/systemd/system/   # 書き換えてから
sudo systemctl daemon-reload
sudo systemctl enable --now slack-mcp.service slack-mcp-admin.service
systemctl is-active slack-mcp.service slack-mcp-admin.service
```

MCP サーバーは `127.0.0.1:8009` (`SLACK_MCP_PORT` で変えられる)、管理画面は `127.0.0.1:8010` (service の `--port`) で待ち受ける。

### 3. リバースプロキシで公開する

`/slack_mcp/mcp/` を `http://127.0.0.1:8009/mcp/` に、`/slack_mcp/admin/` を `http://127.0.0.1:8010/` に中継する。

- `X-Forwarded-For` を付ける。管理画面のログインの失敗の回数制限を、接続元ごとに数えるのに使う
- MCP サーバーには、`Host` ヘッダーを**書き換えて** (中継先の `127.0.0.1:8009` にして) 渡す。元の `Host` のまま渡すと、
  MCP の SDK の DNS リバインディング対策で拒否される。Apache の `ProxyPass` と nginx の `proxy_pass` は、既定でそうなる
- 管理画面は相対パスで API を呼ぶので、どのパスの下でも動く (末尾の `/` は付けて開く)

Apache (`mod_proxy` と `mod_proxy_http` が必要。`X-Forwarded-For` は自動で付く):

```apache
ProxyPass        /slack_mcp/mcp/   http://127.0.0.1:8009/mcp/
ProxyPassReverse /slack_mcp/mcp/   http://127.0.0.1:8009/mcp/
ProxyPass        /slack_mcp/admin/ http://127.0.0.1:8010/
ProxyPassReverse /slack_mcp/admin/ http://127.0.0.1:8010/
```

nginx:

```nginx
location /slack_mcp/mcp/ {
    proxy_pass http://127.0.0.1:8009/mcp/;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
location /slack_mcp/admin/ {
    proxy_pass http://127.0.0.1:8010/;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

管理画面に出すコネクタの URL は、既定では管理画面の URL から決める (`.../admin/` と同じ階層の `.../mcp/<トークン>`)。
MCP サーバーを別の場所 (別のパスやドメイン) で公開するときは、`SLACK_MCP_PUBLIC_URL` (例: `https://example.com/slack_mcp/mcp`) を
`/etc/default/slack-mcp-admin` に書いて管理画面を再起動する。`clients.py` で URL を表示するときも、この環境変数を付けると完全な URL が出る。

### 4. Slack アプリを作り、アカウントを登録する

「[Slack アプリの作成とトークンの取得](#slack-アプリの作成とトークンの取得)」でトークンを取り、
管理画面 (`https://example.com/slack_mcp/admin/`) の **Slack アカウント** で登録する。接続テストで、どのワークスペースのどのユーザー・bot か、
スコープが足りているかを確かめられる。

### 5. AI のコネクタを作り、AI に登録する

管理画面の **AI コネクタ** で AI を追加すると、その AI 専用のコネクタ URL がコピーされる。
claude.ai なら「設定 → コネクタ → カスタムコネクタを追加」で、URL にそれを入れる (認証の設定は不要。URL のトークンが鍵の代わり)。
ほかの AI も同じように、AI ごとに追加した URL を登録する。URL を作り直したら、その AI のコネクタの URL も変える。

### 更新

```bash
cd /opt/slack-mcp && git pull --ff-only
sudo systemctl restart slack-mcp.service slack-mcp-admin.service
```

`requirements.txt` を変えたときは、再起動の前に `.venv/bin/pip install -r requirements.txt` を、
systemd の定義を変えたときは `install` と `daemon-reload` もやり直す。

---

## アカウント

アカウントごとに `accounts/<アカウント名>.toml` が 1 つ (Git 管理外。ユーザートークンを含む)。管理画面で作れるが、手で書いてもよい。

```toml
user_token = "xoxp-…"                    # Slack アプリの User OAuth Token (読み取りと、本人として行う操作)
bot_token = "xoxb-…"                     # 同じアプリの Bot User OAuth Token (AI が bot として投稿する)
label = "個人用 (example@example.com)"   # 任意。一覧に出す説明
read_only = false                        # 任意。true なら送信・変更系のツールを断る (どの AI からでも)
```

- アカウント名は英小文字・数字・ハイフン・アンダースコアで 32 文字まで。MCP の各ツールの `account` にこの名前を指定する (アカウントが 1 つだけなら省略できる)
- ファイルを足したり書き換えたりすると、再起動しなくても次の呼び出しから反映される
- チャンネル ID・ユーザー ID・ts はワークスペースごとに別なので、別のアカウントの ID をそのまま使ってもエラーになる

手で書くときは:

```bash
mkdir -p accounts && chmod 700 accounts
cp account.example.toml accounts/personal.toml && chmod 600 accounts/personal.toml
vi accounts/personal.toml                 # user_token・bot_token を書く (アカウントの数だけ繰り返す)
```

### Slack アプリの作成とトークンの取得

ワークスペースごと (そのワークスペースに入っているアカウントで) に Slack アプリを作り、自分のユーザートークンを取る。
OAuth の画面やサーバー側の処理は要らない。

1. そのワークスペースのアカウントで https://api.slack.com/apps を開き、**Create New App** → **From a manifest** を選ぶ
2. ワークスペースを選び、`slack_app_manifest.json` の中身を貼り付けて作る (ユーザーと bot のスコープが一式入る)
3. 左の **OAuth & Permissions** → **Install to Workspace** で自分のアカウントとして許可する
   (管理者の承認が要るワークスペースでは、承認されるまで待つ)
4. 同じページの **User OAuth Token** (`xoxp-`) を `user_token` に、**Bot User OAuth Token** (`xoxb-`) を `bot_token` に書く

すでにアプリを作ってあるときは、アプリの **App Manifest** に `slack_app_manifest.json` を貼り直して保存し、
**Install App** → **Reinstall to Workspace** する。bot のトークンが出るので `bot_token` に足す (`user_token` が変わっていたらそれも書き換える)。

- bot の名前は、投稿するときに AI ごとの表示名 (`clients.json` の `name`) に置き換わる。アプリの bot の名前 (マニフェストでは "AI") が出るのは、
  予約送信とリアクション (Slack の API が表示名の上書きに対応していない) と、メンバー一覧など
- bot は公開チャンネルにはそのまま投稿できる (`chat:write.public`)。プライベートチャンネルに投稿させたいときは、そのチャンネルで `/invite @アプリ名` する
- bot は持ち主と他人の DM には投稿できない。人に送るときは bot からその人への DM になる

> Slack のレート制限について: 2025 年 5 月以降に作った「Marketplace 外で配布するアプリ」は `conversations.history` / `conversations.replies` が
> 1 分に 1 回・15 件に制限されるが、自分のワークスペース用に作って配布しない内部アプリは対象外 (従来どおり Tier 3)。
> このサーバーは内部アプリとして使う前提。制限にかかったときはツールのエラーにそう出る。

---

## 管理画面

`https://example.com/slack_mcp/admin/` でログインして、次のことができる。保存は再起動しなくても MCP サーバーにすぐ効く。

- **AI コネクタ**: AI の追加・編集・削除、コネクタ URL の表示・コピー・作り直し (表示名・アイコン・投稿方法・使えるアカウント・読み取り専用・本人としての操作の許可)
- **Slack アカウント**: `accounts/<名前>.toml` の作成・編集・削除 (削除は `accounts/.deleted/` へ移す)。トークンは画面に表示せず、空のまま保存すると今のトークンを残す。
  接続テストで、どのワークスペースのどのユーザー・bot か、マニフェストのスコープで足りないものがないかを確かめる
- **Slack アプリの設定**: アプリの作り方と、マニフェストのコピー

- ログイン情報は `/etc/default/slack-mcp-admin` (上の「[2. systemd に登録する](#2-systemd-に登録する)」)。開発時はリポジトリ直下の `idpass.txt` (1 行目がユーザー名、2 行目がパスワード。Git 管理外) でもよい
- Cookie は HttpOnly・SameSite=Strict。service の例では `SLACK_MCP_ADMIN_SECURE_COOKIE=1` で Secure も付ける (HTTPS で公開する前提)。書き込みの API は JSON しか受け付けない (他サイトからのフォーム送信を防ぐ)
- 同じ接続元からログインに 10 分で 10 回失敗すると、しばらく受け付けない

---

## AI ごとのコネクタ (`clients.py`)

管理画面と同じことをコマンドでもできる (リポジトリ直下で実行する)。

AI ごとに 1 つ、コネクタの URL (`.../mcp/<トークン>`) を発行する。設定は `clients.json` (Git 管理外・600)。
追加・変更・作り直しは再起動しなくてもすぐ効く。

```bash
export SLACK_MCP_PUBLIC_URL=https://example.com/slack_mcp/mcp   # 付けると完全な URL を表示する
.venv/bin/python clients.py add claude --name Claude --icon-emoji :robot_face:      # URL が表示される
.venv/bin/python clients.py add chatgpt --name ChatGPT --icon-url https://…/c.png --accounts personal
.venv/bin/python clients.py set chatgpt --read-only        # 読み取りだけにする (--writable で戻す)
.venv/bin/python clients.py set claude --allow-as-user     # Canvas・List などの本人としての操作も許す
.venv/bin/python clients.py set owner --post-as user       # 本人として投稿する AI にする
.venv/bin/python clients.py list
.venv/bin/python clients.py url claude                     # URL を表示する
.venv/bin/python clients.py regenerate claude              # トークンを作り直す (前の URL はすぐ使えなくなる)
.venv/bin/python clients.py remove chatgpt
```

| 設定 | 意味 |
| --- | --- |
| `name` / `icon_emoji` / `icon_url` | bot として投稿するときの表示名とアイコン |
| `post_as` | `bot` (既定) か `user` (本人として投稿) |
| `accounts` | 使ってよいアカウント (空ならすべて) |
| `read_only` | 読み取りだけ |
| `allow_as_user` | bot のコネクタでも、Canvas・List の作成と編集・チャンネルの作成 (本人の名前で行われる) を許す |

`bot` のコネクタで、そのアカウントに `bot_token` がないときは、本人として投稿し直したりせずにエラーにする。

古い版 (`mcp_token` ファイルの 1 つの URL) から移すときは、今の URL のまま引き継げる:

```bash
.venv/bin/python clients.py add claude --name Claude --icon-emoji :robot_face: --token-file mcp_token
rm mcp_token
```

---

## ツール

ツール名と引数は claude.ai の Slack コネクタに合わせてある (すべてに `account` が付く)。

投稿系 (送信・予約・リアクション・ファイルの共有) は、コネクタの `post_as` が `bot` なら bot として、`user` なら本人として行う。
Canvas・List の作成と編集、チャンネルの作成は本人の名前でしかできないので、`bot` のコネクタでは `allow_as_user` を付けたときだけ使える。

| 種類 | ツール |
| --- | --- |
| アカウント | `slack_list_accounts` |
| チャンネル | `slack_list_user_channels` (入っているチャンネル・DM), `slack_search_channels` (名前・説明で探す), `slack_list_channel_members`, `slack_create_conversation` (チャンネル・DM・グループ DM) |
| メッセージを読む | `slack_read_channel`, `slack_read_thread`, `slack_search_messages` (Slack の検索構文), `slack_search_files` |
| ユーザー | `slack_search_users`, `slack_read_user_profile` |
| 送る | `slack_send_message` (Markdown), `slack_schedule_message`, `slack_list_scheduled_messages`, `slack_cancel_scheduled_message`, `slack_add_reaction`, `slack_get_reactions` |
| ファイル | `slack_read_file` (テキストはそのまま、画像は画像で返す), `slack_upload_file` (テキストか Base64。チャンネルへの共有つき) |
| Canvas | `slack_create_canvas`, `slack_read_canvas`, `slack_find_canvas_sections`, `slack_update_canvas` |
| List | `slack_create_list`, `slack_read_list`, `slack_add_list_record`, `slack_update_list_record`, `slack_update_list` |
| 絵文字 | `slack_search_emojis` |

- `channel_id` にはチャンネル ID のほか、ユーザー ID (その人との DM) や `#名前` も渡せる
- 時刻は `SLACK_MCP_TZ` (既定 Asia/Tokyo) の ISO 8601 で返す。`oldest` / `latest` / `post_at` は ts か ISO 8601 (タイムゾーンなしは `SLACK_MCP_TZ`) で指定できる
- メッセージ本文の `<@U…>` `<#C…>` `<url|label>` は名前やリンクに置き換えて返す。送信は `markdown_text` (標準の Markdown) を使う

### claude.ai の Slack コネクタとの違い

- 検索は `keywords` / `filters` の分割ではなく、Slack の検索構文を 1 つの `query` で渡す (`障害 in:#ops after:2026-01-01` など)。公開チャンネルだけに絞る専用ツールはない (`in:` で絞る)
- Canvas の内容を返す公式 API がないので、`slack_read_canvas` はファイルとしてダウンロードして文字にする。形が崩れることがある。セクション ID は `slack_find_canvas_sections` で見出しから探す
- 下書き (`slack_send_message_draft`) は公開 API がないので無い。代わりに内容を持ち主に見せてから `slack_send_message` で送る
- List の列の追加・削除は API にないので、`slack_update_list` は名前・説明・タスクモードだけ変えられる。List は有料プランのみ
- ファイルのアップロードは 1 回のツール呼び出しで終わる (URL 取得 → 送信 → 完了をサーバーがまとめて行う)
- AI ごとのコネクタと bot での投稿はこのサーバー独自。`slack_list_accounts` の `client` で、今のコネクタが bot か本人かが分かる

---

## 環境変数

| キー | 内容 |
| --- | --- |
| `SLACK_MCP_PORT` | MCP サーバーの待ち受けポート (既定 8009。`127.0.0.1` だけで待ち受ける) |
| `SLACK_MCP_PUBLIC_URL` | コネクタの URL の前半 (例 `https://example.com/slack_mcp/mcp`)。空なら管理画面の URL から決める |
| `SLACK_MCP_CLIENTS_FILE` | `clients.json` の場所 (既定はリポジトリ直下) |
| `SLACK_MCP_ACCOUNTS_DIR` | アカウントの設定のディレクトリ (既定はリポジトリ直下の `accounts/`) |
| `SLACK_MCP_TZ` | 時刻の表示と、タイムゾーンなしの時刻の解釈に使うタイムゾーン (既定 `Asia/Tokyo`) |
| `SLACK_MCP_ADMIN_USERNAME` / `SLACK_MCP_ADMIN_PASSWORD` / `SLACK_MCP_ADMIN_SECRET` | 管理画面のログイン情報と Cookie の署名の鍵 |
| `SLACK_MCP_ADMIN_SECURE_COOKIE` | `1` なら管理画面の Cookie に Secure を付ける |
| `SLACK_API_BASE` | Slack API の場所 (試験で偽のサーバーに向けるときだけ使う) |

---

## 開発

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tests/smoke.py        # 偽の Slack に対する通し試験
.venv/bin/python tests/admin_test.py   # 管理画面の API の試験
```

どちらの試験も実際の Slack には接続しない。手元で実際の Slack に向けて動かすときは、`accounts/` にトークンを置いて:

```bash
.venv/bin/python clients.py add local --name Local
.venv/bin/python mcp_server/server.py                                        # http://127.0.0.1:8009/mcp/<トークン>
.venv/bin/python -m uvicorn --app-dir web api:app --host 127.0.0.1 --port 8010   # 管理画面 http://127.0.0.1:8010/
```

送信系のツールは本当に送られるので注意する。

---

## 安全のために

- コネクタの URL はトークンそのもの。漏れたらその AI の分を管理画面か `python clients.py regenerate <id>` で作り直す (前の URL はすぐ使えなくなる)。
  リバースプロキシのアクセスログには URL ごと残る (MCP サーバー自身のログには、AI の id しか残さない)
- 読み取りは持ち主の権限なので、AI はプライベートチャンネルや DM も読める。信用しきれない AI には `--accounts` で使えるアカウントを絞り、`--read-only` にする
- 接続元の AI は URL のトークンだけで決まる (サーバーが入口でヘッダーを付け直すので、外から偽れない)
- 管理画面は Slack のトークンとすべてのコネクタを扱える。パスワードは長いものにする
- `accounts/` と `clients.json` は Git 管理外。サーバーの外に出さない
