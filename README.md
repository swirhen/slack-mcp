# slack-mcp

複数の Slack アカウント (ログインのメールアドレスごと・ワークスペースごと) を、1 つの MCP コネクタで扱うサーバー。
claude.ai の Slack コネクタは 1 つしか持てないので、その代わりに自宅サーバーで動かし、同じようなツールを提供する。

AI (claude.ai・ChatGPT など) ごとに別のコネクタ URL を発行し、投稿は AI ごとの名前・アイコンの bot として行う。
読み取り (履歴・検索など) は持ち主本人の権限で行う。

- リポジトリ: `swirhen/slack-mcp` (非公開)
- 本番の配置先: `/home/swirhen/slack-mcp`
- 公開 URL: Apache 配下の `/slack_mcp/mcp/<トークン>` (`127.0.0.1:8009` へ転送)
- 管理画面: `https://swirhen.tv/slack_mcp/admin/` (`127.0.0.1:8010` へ転送。AI のコネクタと Slack アカウントをここで設定する)

---

## 全体像

```
 claude.ai ─▶ /slack_mcp/mcp/<claude のトークン>  ─┐                          clients.json (AI ごとの名前・アイコン・投稿方法)
 ChatGPT   ─▶ /slack_mcp/mcp/<chatgpt のトークン> ─┼─▶ Apache ─▶ mcp_server/server.py (127.0.0.1:8009, slack-mcp.service)
 …                                                ─┘                 │
                                     accounts/<アカウント>.toml ───────┤ 読む: user_token (xoxp・持ち主本人)
                                                                     └ 投稿: bot_token (xoxb) + AI ごとの表示名・アイコン
```

| ファイル | 役割 |
| --- | --- |
| `mcp_server/server.py` | MCP サーバー本体 (Streamable HTTP)。ツールの定義と、URL のトークンの確認 |
| `workspace.py` | アカウントごとの状態 (自分のユーザー ID・ユーザー名・チャンネル名のキャッシュ) と、Slack の応答の整形 |
| `slack_client.py` | Slack Web API の薄いクライアント (レート制限の再試行、ファイルの送受信) |
| `accounts.py` | `accounts/<アカウント名>.toml` の読み込み |
| `clients.py` | AI ごとのコネクタ (URL のトークン・表示名・アイコン・投稿方法・使えるアカウント) の管理 |
| `web/api.py` / `web/index.html` | 管理画面 (FastAPI と、それが配る 1 枚の HTML。ビルドは要らない) |
| `slack_app_manifest.json` | Slack アプリを作るときのマニフェスト (必要なスコープが入っている) |
| `account.example.toml` | アカウントの設定ファイルの例 |
| `tests/` | 偽の Slack API に対する通し試験 |

---

## アカウント

アカウントごとに `accounts/<アカウント名>.toml` が 1 つ (Git 管理外。ユーザートークンを含む)。

```toml
user_token = "xoxp-…"                    # Slack アプリの User OAuth Token (読み取りと、本人として行う操作)
bot_token = "xoxb-…"                     # 同じアプリの Bot User OAuth Token (AI が bot として投稿する)
label = "個人用 (example@example.com)"   # 任意。一覧に出す説明
read_only = false                        # 任意。true なら送信・変更系のツールを断る (どの AI からでも)
```

- アカウント名は英小文字・数字・ハイフン・アンダースコアで 32 文字まで。MCP の各ツールの `account` にこの名前を指定する (アカウントが 1 つだけなら省略できる)
- ファイルを足したり書き換えたりすると、再起動しなくても次の呼び出しから反映される
- チャンネル ID・ユーザー ID・ts はワークスペースごとに別なので、別のアカウントの ID をそのまま使ってもエラーになる

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

## ツール

ツール名と引数は claude.ai の Slack コネクタに合わせてある (すべてに `account` が付く)。

投稿系 (送信・予約・リアクション・ファイルの共有) は、コネクタの `post_as` が `bot` なら bot として、`user` なら本人として行う。
Canvas・List の作成と編集、チャンネルの作成は本人の名前でしかできないので、`bot` のコネクタでは `allow_as_user` を付けたときだけ使える。

---

## 管理画面

`https://swirhen.tv/slack_mcp/admin/` でログインして、次のことができる。保存は再起動しなくても MCP サーバーにすぐ効く。

- **AI コネクタ**: AI の追加・編集・削除、コネクタ URL の表示・コピー・作り直し (表示名・アイコン・投稿方法・使えるアカウント・読み取り専用・本人としての操作の許可)
- **Slack アカウント**: `accounts/<名前>.toml` の作成・編集・削除 (削除は `accounts/.deleted/` へ移す)。トークンは画面に表示せず、空のまま保存すると今のトークンを残す。
  接続テストで、どのワークスペースのどのユーザー・bot か、マニフェストのスコープで足りないものがないかを確かめる
- **Slack アプリの設定**: アプリの作り方と、マニフェストのコピー

ログイン情報は本番では `/etc/default/slack-mcp-admin` (root 所有・600)、ローカルではリポジトリ直下の `idpass.txt` (1 行目がユーザー名、2 行目がパスワード。Git 管理外)。

```
SLACK_MCP_ADMIN_USERNAME=…
SLACK_MCP_ADMIN_PASSWORD=…
SLACK_MCP_ADMIN_SECRET=…    # Cookie の署名用。長いランダムな文字列 (openssl rand -hex 32)。変えると全員ログアウトする
```

- Cookie は HttpOnly・SameSite=Strict・Secure (本番)。書き込みの API は JSON しか受け付けない (他サイトからのフォーム送信を防ぐ)
- 同じ接続元からログインに 10 分で 10 回失敗すると、しばらく受け付けない
- ローカルで開くとき: `python -m uvicorn --app-dir web api:app --host 127.0.0.1 --port 8010` で `http://127.0.0.1:8010/`

## AI ごとのコネクタ (`clients.py`)

管理画面と同じことをコマンドでもできる。

AI ごとに 1 つ、コネクタの URL (`/slack_mcp/mcp/<トークン>`) を発行する。設定は `clients.json` (Git 管理外・600)。
追加・変更・作り直しは再起動しなくてもすぐ効く。

```bash
.venv/bin/python clients.py add claude --name Claude --icon-emoji :robot_face:      # URL が表示される
.venv/bin/python clients.py add chatgpt --name ChatGPT --icon-url https://…/c.png --accounts swirhen
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
- 時刻は日本時間の ISO 8601 で返す。`oldest` / `latest` / `post_at` は ts か ISO 8601 (タイムゾーンなしは日本時間) で指定できる
- メッセージ本文の `<@U…>` `<#C…>` `<url|label>` は名前やリンクに置き換えて返す。送信は `markdown_text` (標準の Markdown) を使う

### claude.ai の Slack コネクタとの違い

- 検索は `keywords` / `filters` の分割ではなく、Slack の検索構文を 1 つの `query` で渡す (`障害 in:#ops after:2026-01-01` など)。公開チャンネルだけに絞る専用ツールはない (`in:` で絞る)
- Canvas の内容を返す公式 API がないので、`slack_read_canvas` はファイルとしてダウンロードして文字にする。形が崩れることがある。セクション ID は `slack_find_canvas_sections` で見出しから探す
- 下書き (`slack_send_message_draft`) は公開 API がないので無い。代わりに内容を持ち主に見せてから `slack_send_message` で送る
- List の列の追加・削除は API にないので、`slack_update_list` は名前・説明・タスクモードだけ変えられる。List は有料プランのみ
- ファイルのアップロードは 1 回のツール呼び出しで終わる (URL 取得 → 送信 → 完了をサーバーがまとめて行う)
- AI ごとのコネクタと bot での投稿はこのサーバー独自。`slack_list_accounts` の `client` で、今のコネクタが bot か本人かが分かる

---

## ローカルでの実行

```powershell
python -m venv $env:USERPROFILE\.venv\slack-mcp
& $env:USERPROFILE\.venv\slack-mcp\Scripts\python.exe -m pip install -r requirements.txt
& $env:USERPROFILE\.venv\slack-mcp\Scripts\python.exe tests\smoke.py        # 偽の Slack に対する通し試験
& $env:USERPROFILE\.venv\slack-mcp\Scripts\python.exe tests\admin_test.py   # 管理画面の API の試験
& $env:USERPROFILE\.venv\slack-mcp\Scripts\python.exe clients.py add local --name Local
& $env:USERPROFILE\.venv\slack-mcp\Scripts\python.exe mcp_server\server.py   # http://127.0.0.1:8009/mcp/<トークン>
```

環境変数: `SLACK_MCP_PORT` (既定 8009)、`SLACK_MCP_CLIENTS_FILE`、`SLACK_MCP_ACCOUNTS_DIR`、`SLACK_MCP_TZ` (既定 Asia/Tokyo)、
`SLACK_MCP_PUBLIC_URL` (`clients.py` が表示する URL の先頭。既定 `https://swirhen.tv/slack_mcp/mcp`)、`SLACK_API_BASE` (テスト用)。

---

## 本番への導入 (Ubuntu 22.04)

ユーザー `swirhen`、配置先 `/home/swirhen/slack-mcp`、ポート `8009` を前提にしている。

```bash
cd /home/swirhen
git clone git@github.com:swirhen/slack-mcp.git
cd slack-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

mkdir -p accounts && chmod 700 accounts
cp account.example.toml accounts/personal.toml && chmod 600 accounts/personal.toml
vi accounts/personal.toml                 # user_token・bot_token を書く (アカウントの数だけ繰り返す)

.venv/bin/python clients.py add claude --name Claude --icon-emoji :robot_face:   # コネクタの URL が表示される

sudo cp mcp_server/slack-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now slack-mcp.service
systemctl is-active slack-mcp.service
```

管理画面:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/default/slack-mcp-admin
sudo vi /etc/default/slack-mcp-admin      # SLACK_MCP_ADMIN_USERNAME / PASSWORD / SECRET を書く
sudo cp web/slack-mcp-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now slack-mcp-admin.service
systemctl is-active slack-mcp-admin.service
```

Apache は `dotfile/apache2/sites-available/000-default.conf` に `/slack_mcp/mcp/` → `127.0.0.1:8009/mcp/` と、
`/slack_mcp/admin/` → `127.0.0.1:8010/` の転送がある。
本番の `~/dotfile` で `git pull` → `sudo apachectl configtest` → `sudo systemctl reload apache2`。

### claude.ai への登録

設定 → コネクタ → カスタムコネクタを追加 で、URL に `python clients.py url claude` の URL を入れる (認証なし)。
ほかの AI も同じように、それぞれの id で `clients.py add` した URL を登録する。
トークンを作り直したら (`python clients.py regenerate <id>`)、その AI のコネクタの URL も変える。

### 古い版 (mcp_token の 1 つの URL) からの移行

```bash
.venv/bin/python clients.py add claude --name Claude --icon-emoji :robot_face: --token-file mcp_token   # 今の URL のまま引き継ぐ
rm mcp_token
```

### 更新

```bash
ssh prod-checker "cd /home/swirhen/slack-mcp && git pull --ff-only && sudo systemctl restart slack-mcp.service slack-mcp-admin.service && systemctl is-active slack-mcp.service slack-mcp-admin.service"
```

---

## 安全のために

- コネクタの URL はトークンそのもの。漏れたらその AI の分を `python clients.py regenerate <id>` で作り直す (前の URL はすぐ使えなくなる)
- 読み取りは持ち主の権限なので、AI はプライベートチャンネルや DM も読める。信用しきれない AI には `--accounts` で使えるアカウントを絞り、`--read-only` にする
- 接続元の AI は URL のトークンだけで決まる (サーバーが入口でヘッダーを付け直すので、外から偽れない)
- `accounts/` と `clients.json` は Git 管理外。サーバーの外に出さない
