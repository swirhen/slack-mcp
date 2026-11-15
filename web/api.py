"""slack-mcp の管理画面 (FastAPI)。AI ごとのコネクタ (clients.json) と Slack アカウント (accounts/*.toml) を編集する。

画面は同じディレクトリの index.html を / で配る (ビルドは要らない)。API はすべて /api/ の下で、相対パスで呼ぶので、
Apache の /slack_mcp/admin/ の下でも、ローカルの http://127.0.0.1:8010/ でも動く。

ログイン情報は環境変数 SLACK_MCP_ADMIN_USERNAME / SLACK_MCP_ADMIN_PASSWORD / SLACK_MCP_ADMIN_SECRET
(本番は /etc/default/slack-mcp-admin)。ローカルではリポジトリ直下の idpass.txt (1 行目がユーザー名、2 行目がパスワード)。
パスワードか SECRET がないと、誰もログインできない。

MCP サーバーはどちらのファイルも毎回確かめるので、ここで保存すると再起動しなくてもすぐ効く。

起動 (リポジトリ直下で):
  python -m uvicorn --app-dir web api:app --host 127.0.0.1 --port 8010
"""
from pathlib import Path
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

import accounts  # noqa: E402
import clients  # noqa: E402
import slack_client  # noqa: E402
from slack_client import SlackError  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent
MANIFEST_FILE = REPO_DIR / 'slack_app_manifest.json'
IDPASS_FILE = REPO_DIR / 'idpass.txt'
AUTH_COOKIE = 'slack_mcp_admin_session'
SESSION_TTL = 60 * 60 * 24 * 30
LOGIN_WINDOW = 600
LOGIN_MAX_FAILURES = 10


def load_local_credentials() -> tuple[str, str]:
    try:
        lines = IDPASS_FILE.read_text(encoding='utf-8').splitlines()
    except OSError:
        return 'admin', ''
    return (lines[0].strip() if lines else 'admin', lines[1].strip() if len(lines) > 1 else '')


LOCAL_USERNAME, LOCAL_PASSWORD = load_local_credentials()
USERNAME = os.getenv('SLACK_MCP_ADMIN_USERNAME', LOCAL_USERNAME)
PASSWORD = os.getenv('SLACK_MCP_ADMIN_PASSWORD', LOCAL_PASSWORD)
SECRET = os.getenv('SLACK_MCP_ADMIN_SECRET', 'local-debug-secret' if IDPASS_FILE.is_file() else '')
SECURE_COOKIE = os.getenv('SLACK_MCP_ADMIN_SECURE_COOKIE', '') in ('1', 'true', 'yes')

app = FastAPI(title='slack-mcp admin', docs_url=None, redoc_url=None, openapi_url=None)
_failures: dict[str, list[float]] = {}


# ---------------- 認証・共通 ----------------
def make_session_token(issued_at: int) -> str:
    token = hmac.new(SECRET.encode(), f'{issued_at}:{USERNAME}'.encode(), hashlib.sha256).hexdigest()
    return f'{issued_at}:{token}'


def verify_session_token(value: str | None) -> bool:
    if not PASSWORD or not SECRET or not value:
        return False
    try:
        issued_at = int(value.split(':', 1)[0])
    except ValueError:
        return False
    if time.time() - issued_at > SESSION_TTL:
        return False
    return hmac.compare_digest(value, make_session_token(issued_at))


def require_auth(authorization: str | None = Header(default=None),
                 session: str | None = Cookie(default=None, alias=AUTH_COOKIE)):
    if authorization and authorization.startswith('Bearer ') and verify_session_token(authorization[7:].strip()):
        return
    if verify_session_token(session):
        return
    raise HTTPException(401, 'ログインが必要です')


async def json_body(request: Request) -> dict:
    # 他サイトからのフォーム送信 (CSRF) を防ぐため、JSON 以外は受け付けない
    if not request.headers.get('content-type', '').startswith('application/json'):
        raise HTTPException(415, 'Content-Type が application/json ではありません')
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, 'JSON を読めません') from None
    if not isinstance(data, dict):
        raise HTTPException(400, 'JSON のオブジェクトを送ってください')
    return data


def client_ip(request: Request) -> str:
    forwarded = request.headers.get('x-forwarded-for', '')
    return forwarded.split(',')[0].strip() or (request.client.host if request.client else '-')


@app.exception_handler(ValueError)
async def value_error_handler(request, exc):
    return JSONResponse(status_code=400, content={'detail': str(exc)})


@app.exception_handler(KeyError)
async def key_error_handler(request, exc):
    return JSONResponse(status_code=404, content={'detail': f'{exc.args[0] if exc.args else ""} が見つかりません'})


AUTH = [Depends(require_auth)]


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get('/')
def index():
    return FileResponse(WEB_DIR / 'index.html', headers={'Cache-Control': 'no-store'})


@app.get('/api/health')
def health():
    return {'status': 'ok'}


@app.get('/api/session', dependencies=AUTH)
def session():
    return {'authenticated': True, 'username': USERNAME}


@app.post('/api/login')
def login(payload: LoginRequest, request: Request, response: Response):
    ip = client_ip(request)
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < LOGIN_WINDOW]
    if len(recent) >= LOGIN_MAX_FAILURES:
        raise HTTPException(429, '失敗が続いたので、しばらく待ってからやり直してください')
    if not PASSWORD or not SECRET or not hmac.compare_digest(payload.username, USERNAME) \
            or not hmac.compare_digest(payload.password, PASSWORD):
        _failures[ip] = recent + [now]
        raise HTTPException(401, 'ユーザー名またはパスワードが違います')
    _failures.pop(ip, None)
    value = make_session_token(int(now))
    response.set_cookie(AUTH_COOKIE, value, httponly=True, samesite='strict', secure=SECURE_COOKIE, max_age=SESSION_TTL)
    return {'authenticated': True}


@app.post('/api/logout')
def logout(response: Response):
    response.delete_cookie(AUTH_COOKIE)
    return {'authenticated': False}


# ---------------- AI ごとのコネクタ ----------------
CLIENT_FIELDS = clients.OPTION_KEYS


def client_options(data: dict) -> dict:
    return {k: data[k] for k in CLIENT_FIELDS if k in data}


@app.get('/api/clients', dependencies=AUTH)
def list_clients():
    return {'clients': [clients.client_view(c) for c in clients.load_clients()]}


@app.post('/api/clients', dependencies=AUTH)
def add_client(data: dict = Depends(json_body)):
    return clients.create_client(str(data.get('id', '')).strip(), **client_options(data))


@app.put('/api/clients/{cid}', dependencies=AUTH)
def put_client(cid: str, data: dict = Depends(json_body)):
    return clients.update_client(cid, **client_options(data))


@app.post('/api/clients/{cid}/regenerate', dependencies=AUTH)
def regenerate(cid: str, _: dict = Depends(json_body)):
    return clients.regenerate_client(cid)


@app.delete('/api/clients/{cid}', dependencies=AUTH)
def delete_client(cid: str, _: dict = Depends(json_body)):
    clients.remove_client(cid)
    return {'ok': True}


# ---------------- Slack アカウント ----------------
def mask(token: str | None) -> str | None:
    """トークンは画面に返さない。種類と末尾 4 文字だけ見せる"""
    if not token:
        return None
    return f'{token[:5]}…{token[-4:]}'


def account_view(name: str) -> dict:
    try:
        c = accounts.read_account(name)
    except accounts.AccountError as e:
        return {'name': name, 'error': str(e)}
    return {'name': name, 'label': c['label'], 'read_only': c['read_only'],
            'user_token': mask(c['user_token']), 'bot_token': mask(c['bot_token'])}


@app.get('/api/accounts', dependencies=AUTH)
def list_accounts():
    return {'accounts': [account_view(n) for n in accounts.list_accounts()]}


def account_error(e: accounts.AccountError):
    return HTTPException(400, str(e))


@app.post('/api/accounts', dependencies=AUTH)
def add_account(data: dict = Depends(json_body)):
    name = str(data.get('name', '')).strip()
    try:
        accounts.write_account(name, user_token=data.get('user_token', ''), bot_token=data.get('bot_token'),
                               label=data.get('label', ''), read_only=bool(data.get('read_only')), create=True)
    except accounts.AccountError as e:
        raise account_error(e) from e
    return account_view(name)


@app.put('/api/accounts/{name}', dependencies=AUTH)
def put_account(name: str, data: dict = Depends(json_body)):
    """トークンは空なら今のまま。bot_token を消すときは clear_bot_token を true にする"""
    try:
        current = accounts.read_account(name)
        bot_token = None if data.get('clear_bot_token') else (data.get('bot_token') or current['bot_token'])
        accounts.write_account(name, user_token=data.get('user_token') or current['user_token'], bot_token=bot_token,
                               label=data.get('label', current['label']),
                               read_only=bool(data.get('read_only', current['read_only'])))
    except accounts.AccountError as e:
        raise account_error(e) from e
    return account_view(name)


@app.delete('/api/accounts/{name}', dependencies=AUTH)
def remove_account(name: str, _: dict = Depends(json_body)):
    try:
        dest = accounts.delete_account(name)
    except accounts.AccountError as e:
        raise account_error(e) from e
    return {'ok': True, 'moved_to': str(dest.relative_to(accounts.ACCOUNTS_DIR))}


def manifest_scopes() -> dict[str, list[str]]:
    try:
        scopes = json.loads(MANIFEST_FILE.read_text(encoding='utf-8'))['oauth_config']['scopes']
    except (OSError, ValueError, KeyError):
        return {'user': [], 'bot': []}
    return {'user': scopes.get('user', []), 'bot': scopes.get('bot', [])}


async def check_token(token: str, kind: str) -> dict:
    try:
        body, scopes = await slack_client.auth_test(token)
    except SlackError as e:
        return {'ok': False, 'error': e.error}
    wanted = manifest_scopes()[kind]
    out = {'ok': True, 'team': body.get('team'), 'team_id': body.get('team_id'), 'url': body.get('url'),
           'user': body.get('user'), 'user_id': body.get('user_id')}
    if scopes:
        out['missing_scopes'] = [s for s in wanted if s not in scopes]
    if kind == 'bot' and not body.get('bot_id'):
        out = {'ok': False, 'error': 'bot のトークンではない'}
    return out


@app.post('/api/accounts/test', dependencies=AUTH)
async def test_tokens(data: dict = Depends(json_body)):
    """保存前のトークン、または保存済みのアカウント (name) のトークンで接続を確かめる。入力したトークンが優先"""
    user_token = (data.get('user_token') or '').strip()
    bot_token = (data.get('bot_token') or '').strip()
    if data.get('name'):
        try:
            current = accounts.read_account(str(data['name']))
        except accounts.AccountError as e:
            raise account_error(e) from e
        user_token = user_token or current['user_token']
        bot_token = bot_token or (current['bot_token'] or '')
    if not user_token:
        raise HTTPException(400, 'user_token を入れてください')
    tasks = [check_token(user_token, 'user')] + ([check_token(bot_token, 'bot')] if bot_token else [])
    results = await asyncio.gather(*tasks)
    out = {'user': results[0], 'bot': results[1] if bot_token else None}
    if out['bot'] and out['bot'].get('ok') and out['user'].get('ok') and out['bot']['team_id'] != out['user']['team_id']:
        out['warning'] = 'user_token と bot_token のワークスペースが違う'
    return out


@app.get('/api/manifest', dependencies=AUTH)
def manifest():
    return {'manifest': MANIFEST_FILE.read_text(encoding='utf-8')}
