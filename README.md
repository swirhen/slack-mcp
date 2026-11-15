# slack-mcp

複数の Slack アカウント (ログインのメールアドレスごと・ワークスペースごと) を、1 つの MCP コネクタで扱うサーバー。
claude.ai の Slack コネクタは 1 つしか持てないので、その代わりに自宅サーバーで動かし、同じようなツールを提供する。

- リポジトリ: `swirhen/slack-mcp` (非公開)
- 本番の配置先: `/home/swirhen/slack-mcp`
- 公開 URL: Apache 配下の `/slack_mcp/mcp/<トークン>` (`127.0.0.1:8009` へ転送)

---

## 全体像

```
 claude.ai など ──▶ https://swirhen.tv/slack_mcp/mcp/<トークン> ──▶ Apache ──▶ mcp_server/server.py (127.0.0.1:8009)
                                                                                  │ systemd: slack-mcp.service
                                                                                  ├─ accounts/personal.toml  (xoxp-…)  ──▶ ワークスペース A (本人として)
                                                                                  ├─ accounts/work.toml      (xoxp-…)  ──▶ ワークスペース B
                                                                                  └─ accounts/<名前>.toml    (xoxp-…)  ──▶ …
```

| ファイル | 役割 |
| --- | --- |
| `mcp_server/server.py` | MCP サーバー本体 (Streamable HTTP)。ツールの定義と、URL のトークンの確認 |
| `workspace.py` | アカウントごとの状態 (自分のユーザー ID・ユーザー名・チャンネル名のキャッシュ) と、Slack の応答の整形 |
| `slack_client.py` | Slack Web API の薄いクライアント (レート制限の再試行、ファイルの送受信) |
| `accounts.py` | `accounts/<アカウント名>.toml` の読み込み |
| `mcp_token.py` | コネクタ URL のトークンの発行・確認 |
| `slack_app_manifest.json` | Slack アプリを作るときのマニフェスト (必要なスコープが入っている) |
| `account.example.toml` | アカウントの設定ファイルの例 |
| `tests/` | 偽の Slack API に対する通し試験 |

---

## アカウント

アカウントごとに `accounts/<アカウント名>.toml` が 1 つ (Git 管理外。ユーザートークンを含む)。

```toml
user_token = "xoxp-…"                    # Slack アプリの User OAuth Token
label = "個人用 (example@example.com)"   # 任意。一覧に出す説明
read_only = false                        # 任意。true なら送信・変更系のツールを断る
```

- アカウント名は英小文字・数字・ハイフン・アンダースコアで 32 文字まで。MCP の各ツールの `account` にこの名前を指定する (アカウントが 1 つだけなら省略できる)
- ファイルを足したり書き換えたりすると、再起動しなくても次の呼び出しから反映される
- チャンネル ID・ユーザー ID・ts はワークスペースごとに別なので、別のアカウントの ID をそのまま使ってもエラーになる

### Slack アプリの作成とトークンの取得

ワークスペースごと (そのワークスペースに入っているアカウントで) に Slack アプリを作り、自分のユーザートークンを取る。
OAuth の画面やサーバー側の処理は要らない。

1. そのワークスペースのアカウントで https://api.slack.com/apps を開き、**Create New App** → **From a manifest** を選ぶ
2. ワークスペースを選び、`slack_app_manifest.json` の中身を貼り付けて作る (ユーザースコープが一式入る)
3. 左の **OAuth & Permissions** → **Install to Workspace** で自分のアカウントとして許可する
   (管理者の承認が要るワークスペースでは、承認されるまで待つ)
4. 同じページの **User OAuth Token** (`xoxp-` で始まる) を `accounts/<アカウント名>.toml` の `user_token` に書く

スコープを足したいときは、アプリの **OAuth & Permissions** でスコープを足して **Reinstall** すると新しいトークンになる (ファイルも書き換える)。

> Slack のレート制限について: 2025 年 5 月以降に作った「Marketplace 外で配布するアプリ」は `conversations.history` / `conversations.replies` が
> 1 分に 1 回・15 件に制限されるが、自分のワークスペース用に作って配布しない内部アプリは対象外 (従来どおり Tier 3)。
> このサーバーは内部アプリとして使う前提。制限にかかったときはツールのエラーにそう出る。

---

## ツール

ツール名と引数は claude.ai の Slack コネクタに合わせてある (すべてに `account` が付く)。

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

---

## ローカルでの実行

```powershell
python -m venv $env:USERPROFILE\.venv\slack-mcp
& $env:USERPROFILE\.venv\slack-mcp\Scripts\python.exe -m pip install -r requirements.txt
& $env:USERPROFILE\.venv\slack-mcp\Scripts\python.exe tests\smoke.py        # 偽の Slack に対する通し試験
& $env:USERPROFILE\.venv\slack-mcp\Scripts\python.exe mcp_token.py --generate
& $env:USERPROFILE\.venv\slack-mcp\Scripts\python.exe mcp_server\server.py   # http://127.0.0.1:8009/mcp/<トークン>
```

環境変数: `SLACK_MCP_PORT` (既定 8009)、`SLACK_MCP_TOKEN_FILE`、`SLACK_MCP_ACCOUNTS_DIR`、`SLACK_MCP_TZ` (既定 Asia/Tokyo)、`SLACK_API_BASE` (テスト用)。

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
vi accounts/personal.toml                 # user_token を書く (アカウントの数だけ繰り返す)

.venv/bin/python mcp_token.py --generate  # コネクタの URL が表示される

sudo cp mcp_server/slack-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now slack-mcp.service
systemctl is-active slack-mcp.service
```

Apache は `dotfile/apache2/sites-available/000-default.conf` に `/slack_mcp/mcp/` → `127.0.0.1:8009/mcp/` の転送がある。
本番の `~/dotfile` で `git pull` → `sudo apachectl configtest` → `sudo systemctl reload apache2`。

### claude.ai への登録

設定 → コネクタ → カスタムコネクタを追加 で、URL に `https://swirhen.tv/slack_mcp/mcp/<トークン>` を入れる (認証なし)。
トークンを作り直したら (`python mcp_token.py --generate`)、コネクタの URL も変える。

### 更新

```bash
ssh prod-checker "cd /home/swirhen/slack-mcp && git pull --ff-only && sudo systemctl restart slack-mcp.service && systemctl is-active slack-mcp.service"
```

---

## 安全のために

- コネクタの URL はトークンそのもの。漏れたら `python mcp_token.py --generate` で作り直す (前の URL はすぐ使えなくなる)
- ユーザートークンでの操作は、すべて本人の操作として相手に見える。信用しきれない AI には `read_only = true` のアカウントだけ渡す
- `accounts/` と `mcp_token` は Git 管理外。サーバーの外に出さない
