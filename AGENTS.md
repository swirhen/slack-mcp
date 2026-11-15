# AI開発エージェント向け運用ルール

## プロジェクト概要

- 複数の Slack アカウント (ログインのメールアドレスごと・ワークスペースごと) を 1 つの MCP コネクタで扱うサーバー。claude.ai の Slack コネクタは 1 つしか持てないので、その代わりに自宅サーバーで動かす。
- Slack はアカウントごとのユーザートークン (xoxp) で、本人として読み書きする。トークンは `accounts/<アカウント名>.toml` (Git 管理外)。各ツールの `account` でアカウントを選ぶ (1 つだけなら省略できる)。
- MCP サーバー (`mcp_server/server.py`) は systemd の `slack-mcp.service` (`127.0.0.1:8009`) で動かし、Apache の `/slack_mcp/mcp/` で公開する。コネクタの URL は `/slack_mcp/mcp/<トークン>` で、トークンはリポジトリ直下の `mcp_token` (Git 管理外。`python mcp_token.py --generate` で作り直せる。サーバーは毎回ファイルを確かめるので再起動は要らない)。
- ツール名は claude.ai の Slack コネクタに合わせている (`slack_read_channel`, `slack_send_message` など)。

## 主なファイル

- `mcp_server/server.py`: ツールの定義 (Streamable HTTP の MCP サーバー) と `/mcp/<トークン>` の確認。
- `workspace.py`: アカウントごとの状態 (auth.test・ユーザー・チャンネルのキャッシュ) と、メッセージ・ユーザー・ファイルの整形、時刻・rich_text の変換。
- `slack_client.py`: Slack Web API の薄いクライアント (httpx)。レート制限の再試行、ファイルのダウンロード・アップロード。`slack_sdk` は使わない。
- `accounts.py`: `accounts/*.toml` の読み込み (変更を検知して読み直す)。
- `mcp_token.py`: コネクタ URL のトークンの読み書き。
- `slack_app_manifest.json`: Slack アプリを作るときのマニフェスト (必要なユーザースコープの一覧)。
- `tests/fake_slack.py` / `tests/smoke.py`: 偽の Slack API に対する通し試験。

## 開発と検証

- Python の venv はリポジトリ外の `~\.venv\slack-mcp` に PC ごとに作る (ソースは OneDrive で PC 間共有するため)。
  作成: `python -m venv $env:USERPROFILE\.venv\slack-mcp; & $env:USERPROFILE\.venv\slack-mcp\Scripts\python.exe -m pip install -r requirements.txt`
- 本番は Ubuntu 22.04 (Python 3.10)。3.11 以降の機能 (`tomllib` 直接など) に依存しないこと。
- 変更後は `python -m py_compile accounts.py slack_client.py workspace.py mcp_token.py mcp_server/server.py` と、通し試験 `python tests/smoke.py` (実際の Slack には接続しない) を実行する。
- ツールを増やしたら `tests/smoke.py` のツール数の確認と、偽サーバー (`tests/fake_slack.py`) の対応メソッドも足す。
- ローカルで実際の Slack に向けて試すときは、`accounts/` にトークンを置き、`python mcp_token.py --generate` のあと `python mcp_server/server.py` で起動する (`http://127.0.0.1:8009/mcp/<トークン>`)。送信系のツールは本当に送られるので注意。

## 本番リリース

- GitHub へプッシュした後: `ssh prod-checker "cd /home/swirhen/slack-mcp && git pull --ff-only && sudo systemctl restart slack-mcp.service && systemctl is-active slack-mcp.service"`
- 依存パッケージを変えたときは、再起動の前に `.venv/bin/pip install -r requirements.txt` も実行する。
- 初回の導入手順は `README.md` の「本番への導入」を参照。

## 注意

- `accounts/` と `mcp_token` は認証情報を含む。内容を表示・コミットしない。
- ユーザートークンの操作はすべて本人の操作として相手に見える。試験は `tests/smoke.py` (偽サーバー) で行い、本物のワークスペースにテスト投稿をしない。
- Slack API の制約: 検索 (`search.*`) はユーザートークン専用。Canvas の内容を返す公式 API はないので、`slack_read_canvas` はファイルとしてダウンロードして文字にしている (形が崩れることがある)。List は有料プランのみ。下書き (`slack_send_message_draft`) は公開 API がないので無い。
