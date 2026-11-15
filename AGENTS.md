# AI開発エージェント向け運用ルール

## プロジェクト概要

- 複数の Slack アカウント (ログインのメールアドレスごと・ワークスペースごと) を 1 つの MCP コネクタで扱うサーバー。claude.ai の Slack コネクタは 1 つしか持てないので、その代わりに自宅サーバーで動かす。
- Slack はアカウントごとに、ユーザートークン (xoxp) で読み、bot トークン (xoxb) で AI ごとの表示名・アイコンを付けて投稿する (`chat:write.customize`)。トークンは `accounts/<アカウント名>.toml` (Git 管理外)。各ツールの `account` でアカウントを選ぶ (1 つだけなら省略できる)。
- AI ごとにコネクタの URL を分ける。設定は `clients.json` (Git 管理外。`clients.py` で追加・変更・作り直し。サーバーは毎回ファイルを確かめるので再起動は要らない)。AI ごとに `post_as` (bot / user)、表示名・アイコン、使えるアカウント、読み取り専用、本人としての操作 (Canvas・List・チャンネル作成) の許可を持つ。
- 接続元の AI は入口 (`connector_app`) で URL のトークンから決め、`x-slack-mcp-client` ヘッダーに付け直してツールに渡す (外から来た同じヘッダーは捨てる)。ツールは `ctx: Context` を受け取り、`tool_errors` がそれを contextvar に入れる。新しいツールにも必ず `ctx: Context` を付ける。
- bot の設定の AI で `bot_token` がないアカウントには、本人として投稿し直さずにエラーにする。
- MCP サーバー (`mcp_server/server.py`) は systemd の `slack-mcp.service` (`127.0.0.1:8009`) で動かし、Apache の `/slack_mcp/mcp/` で公開する。コネクタの URL は `/slack_mcp/mcp/<トークン>`。
- 管理画面 (`web/api.py` の FastAPI が `web/index.html` を配る。ビルドなし) は `slack-mcp-admin.service` (`127.0.0.1:8010`) で動かし、Apache の `/slack_mcp/admin/` で公開する。AI のコネクタ (`clients.json`) と `accounts/*.toml` を書く。ログイン情報は本番 `/etc/default/slack-mcp-admin`、ローカル `idpass.txt`。画面は相対パス (`api/...`) で API を呼ぶので、どのパスの下でも動く。
- ツール名は claude.ai の Slack コネクタに合わせている (`slack_read_channel`, `slack_send_message` など)。

## 主なファイル

- `mcp_server/server.py`: ツールの定義 (Streamable HTTP の MCP サーバー) と `/mcp/<トークン>` の確認。
- `workspace.py`: アカウントごとの状態 (auth.test・ユーザー・チャンネルのキャッシュ) と、メッセージ・ユーザー・ファイルの整形、時刻・rich_text の変換。
- `slack_client.py`: Slack Web API の薄いクライアント (httpx)。レート制限の再試行、ファイルのダウンロード・アップロード。`slack_sdk` は使わない。
- `accounts.py`: `accounts/*.toml` の読み込み (変更を検知して読み直す)。
- `clients.py`: AI ごとのコネクタの設定の読み書き (`create_client` などは管理画面も使う) と、管理のコマンド (`add` / `set` / `list` / `url` / `regenerate` / `remove`)。
- `web/api.py` / `web/index.html`: 管理画面の API と画面。`accounts.py` の `write_account` / `delete_account` でアカウントの toml を書く。
- `slack_app_manifest.json`: Slack アプリを作るときのマニフェスト (必要なユーザースコープの一覧)。
- `tests/fake_slack.py` / `tests/smoke.py`: 偽の Slack API に対する通し試験。

## 開発と検証

- Python の venv はリポジトリ外の `~\.venv\slack-mcp` に PC ごとに作る (ソースは OneDrive で PC 間共有するため)。
  作成: `python -m venv $env:USERPROFILE\.venv\slack-mcp; & $env:USERPROFILE\.venv\slack-mcp\Scripts\python.exe -m pip install -r requirements.txt`
- 本番は Ubuntu 22.04 (Python 3.10)。3.11 以降の機能 (`tomllib` 直接など) に依存しないこと。
- 変更後は `python -m py_compile accounts.py slack_client.py workspace.py clients.py mcp_server/server.py web/api.py` と、通し試験 `python tests/smoke.py`・管理画面の試験 `python tests/admin_test.py` (どちらも実際の Slack には接続しない) を実行する。
- ツールを増やしたら `tests/smoke.py` のツール数の確認と、偽サーバー (`tests/fake_slack.py`) の対応メソッドも足す。偽サーバーは xoxb のトークンを bot として扱う。
- ローカルで実際の Slack に向けて試すときは、`accounts/` にトークンを置き、`python clients.py add local --name Local` のあと `python mcp_server/server.py` で起動する (`http://127.0.0.1:8009/mcp/<トークン>`)。送信系のツールは本当に送られるので注意。

## 本番リリース

- GitHub へプッシュした後: `ssh prod-checker "cd /home/swirhen/slack-mcp && git pull --ff-only && sudo systemctl restart slack-mcp.service slack-mcp-admin.service && systemctl is-active slack-mcp.service slack-mcp-admin.service"`
- 依存パッケージを変えたときは、再起動の前に `.venv/bin/pip install -r requirements.txt` も実行する。
- 初回の導入手順は `README.md` の「本番への導入」を参照。

## 注意

- `accounts/`・`clients.json`・`idpass.txt`・`/etc/default/slack-mcp-admin` は認証情報を含む。内容を表示・コミットしない。
- ユーザートークンの操作はすべて本人の操作として相手に見える。試験は `tests/smoke.py` (偽サーバー) で行い、本物のワークスペースにテスト投稿をしない。
- Slack API の制約: 検索 (`search.*`) はユーザートークン専用。Canvas の内容を返す公式 API はないので、`slack_read_canvas` はファイルとしてダウンロードして文字にしている (形が崩れることがある)。List は有料プランのみ。下書き (`slack_send_message_draft`) は公開 API がないので無い。bot の表示名の上書き (`username` / `icon_*`) は `chat.postMessage` と `files.completeUploadExternal` だけで効き、予約送信とリアクションはアプリの bot の名前になる。
