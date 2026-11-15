"""管理画面の API の試験。偽の Slack API を立て、FastAPI の TestClient で呼ぶ。

実行 (リポジトリ直下で):  python tests/admin_test.py
実際の Slack には接続しない。
"""
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import time

import uvicorn

ROOT = pathlib.Path(__file__).resolve().parent.parent
SLACK_PORT = 18098
tmp = pathlib.Path(tempfile.mkdtemp(prefix='slack-mcp-admin-test-'))
os.environ.update({
    'SLACK_API_BASE': f'http://127.0.0.1:{SLACK_PORT}/api',
    'SLACK_MCP_ACCOUNTS_DIR': str(tmp / 'accounts'),
    'SLACK_MCP_CLIENTS_FILE': str(tmp / 'clients.json'),
    'SLACK_MCP_PUBLIC_URL': 'https://example.com/slack_mcp/mcp',
    'SLACK_MCP_ADMIN_USERNAME': 'admin',
    'SLACK_MCP_ADMIN_PASSWORD': 'pass-word',
    'SLACK_MCP_ADMIN_SECRET': 'test-secret',
})
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'web'))
sys.path.insert(0, str(ROOT / 'tests'))
import fake_slack  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import accounts  # noqa: E402
import api  # noqa: E402
import clients  # noqa: E402

failures = []


def check(name, cond, info=''):
    print(('  ok   ' if cond else '  FAIL ') + name + (f'  {info}' if info and not cond else ''))
    if not cond:
        failures.append(name)


def run():
    c = TestClient(api.app)
    J = {'Content-Type': 'application/json'}

    # ---- 画面とログイン ----
    r = c.get('/')
    check('index served', r.status_code == 200 and 'slack-mcp 管理' in r.text)
    check('api needs login', c.get('/api/clients').status_code == 401)
    r = c.post('/api/login', json={'username': 'admin', 'password': 'wrong'})
    check('wrong password 401', r.status_code == 401)
    for _ in range(9):
        c.post('/api/login', json={'username': 'admin', 'password': 'wrong'})
    r = c.post('/api/login', json={'username': 'admin', 'password': 'pass-word'})
    check('login rate limited after 10 failures', r.status_code == 429, r.status_code)
    api._failures.clear()
    r = c.post('/api/login', json={'username': 'admin', 'password': 'pass-word'})
    check('login ok, cookie set', r.status_code == 200 and api.AUTH_COOKIE in c.cookies, r.text)
    sc = r.headers.get('set-cookie', '').lower()
    check('cookie httponly + samesite strict', 'httponly' in sc and 'samesite=strict' in sc, sc)
    check('session', c.get('/api/session').json()['username'] == 'admin')

    # ---- CSRF: JSON 以外の書き込みは断る ----
    r = c.post('/api/clients', data={'id': 'x', 'name': 'X'})
    check('form post rejected (415)', r.status_code == 415, r.status_code)

    # ---- アカウント ----
    r = c.post('/api/accounts', json={'name': 'team-a', 'label': '個人 "A" \\ テスト', 'user_token': 'xoxp-test-a', 'bot_token': 'xoxb-test-a'})
    check('account created', r.status_code == 200 and r.json()['bot_token'] == 'xoxb-…st-a', r.text)
    path = tmp / 'accounts' / 'team-a.toml'
    cfg = accounts.read_account('team-a')
    check('toml readable with escaped label', cfg['label'] == '個人 "A" \\ テスト' and cfg['bot_token'] == 'xoxb-test-a', cfg)
    if os.name != 'nt':
        check('toml mode 600', stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode))
    r = c.get('/api/accounts')
    body = r.text
    check('tokens never returned in full', 'xoxp-test-a' not in body and 'xoxb-test-a' not in body, body)
    r = c.post('/api/accounts', json={'name': 'team-a', 'user_token': 'xoxp-test-a'})
    check('duplicate account refused', r.status_code == 400 and 'すでにある' in r.json()['detail'], r.text)
    r = c.post('/api/accounts', json={'name': 'Bad Name', 'user_token': 'xoxp-test-a'})
    check('bad account name refused', r.status_code == 400, r.text)
    r = c.post('/api/accounts', json={'name': 'team-x', 'user_token': 'xoxb-test-a'})
    check('bot token in user field refused', r.status_code == 400 and 'xoxp-' in r.json()['detail'], r.text)
    c.post('/api/accounts', json={'name': 'team-b', 'user_token': 'xoxp-test-b'})

    r = c.post('/api/accounts/test', json={'name': 'team-a'})
    t = r.json()
    check('test saved account: user + bot ok', t['user']['ok'] and t['user']['team'] == 'Team A' and t['bot']['ok']
          and t['bot']['user'] == 'ai_bot' and t['user']['missing_scopes'] == [] and t['bot']['missing_scopes'] == [], t)
    t = c.post('/api/accounts/test', json={'name': 'team-b'}).json()
    check('test detects missing scope', t['user']['missing_scopes'] == ['lists:write'] and t['bot'] is None, t)
    t = c.post('/api/accounts/test', json={'user_token': 'xoxp-test-b', 'bot_token': 'xoxb-test-a'}).json()
    check('test unsaved tokens: workspace mismatch warned', 'ワークスペースが違う' in t.get('warning', ''), t)
    t = c.post('/api/accounts/test', json={'user_token': 'xoxp-nope'}).json()
    check('test invalid token', t['user'] == {'ok': False, 'error': 'invalid_auth'}, t)
    t = c.post('/api/accounts/test', json={'user_token': 'xoxp-test-a', 'bot_token': 'xoxp-test-a'}).json()
    check('test user token in bot field', t['bot']['ok'] is False, t)

    r = c.put('/api/accounts/team-a', json={'label': '新しい説明', 'read_only': True})
    cfg = accounts.read_account('team-a')
    check('update keeps tokens when blank', cfg['user_token'] == 'xoxp-test-a' and cfg['bot_token'] == 'xoxb-test-a'
          and cfg['label'] == '新しい説明' and cfg['read_only'] is True, cfg)
    c.put('/api/accounts/team-a', json={'clear_bot_token': True, 'read_only': False})
    check('clear bot token', accounts.read_account('team-a')['bot_token'] is None)
    c.put('/api/accounts/team-a', json={'bot_token': 'xoxb-test-a'})
    check('set bot token again', accounts.read_account('team-a')['bot_token'] == 'xoxb-test-a')
    r = c.put('/api/accounts/nope', json={'label': 'x'})
    check('update missing account 400', r.status_code == 400, r.text)

    # ---- AI コネクタ ----
    r = c.post('/api/clients', json={'id': 'claude', 'name': 'Claude', 'icon_emoji': 'robot_face', 'accounts': ['team-a']})
    cl = r.json()
    check('client created with url', r.status_code == 200 and cl['url'].startswith('https://example.com/slack_mcp/mcp/')
          and cl['icon_emoji'] == ':robot_face:' and cl['post_as'] == 'bot' and cl['accounts'] == ['team-a'], r.text)
    saved_base, clients.PUBLIC_BASE = clients.PUBLIC_BASE, ''
    check('relative url without public url', clients.connector_url(cl) == '../mcp/' + cl['token'])
    clients.PUBLIC_BASE = saved_base
    found = clients.find_by_token(cl['token'])
    check('MCP side sees new client immediately', found is not None and found['id'] == 'claude', found)
    r = c.post('/api/clients', json={'id': 'claude', 'name': 'X'})
    check('duplicate client refused', r.status_code == 400, r.text)
    r = c.post('/api/clients', json={'id': 'gpt', 'name': 'GPT', 'icon_url': 'http://insecure'})
    check('non-https icon refused', r.status_code == 400 and 'https' in r.json()['detail'], r.text)
    r = c.post('/api/clients', json={'id': 'gpt', 'name': 'GPT', 'post_as': 'admin'})
    check('bad post_as refused', r.status_code == 400, r.text)
    r = c.put('/api/clients/claude', json={'icon_url': 'https://example.com/c.png', 'read_only': True, 'accounts': []})
    cl2 = r.json()
    check('client updated (icon swap, all accounts)', cl2['icon_url'] == 'https://example.com/c.png' and cl2['icon_emoji'] is None
          and cl2['read_only'] and cl2['accounts'] == [] and cl2['token'] == cl['token'], cl2)
    r = c.post('/api/clients/claude/regenerate', json={})
    check('regenerate changes token', r.json()['token'] != cl['token'] and clients.find_by_token(cl['token']) is None, r.text)
    r = c.put('/api/clients/nope', json={'name': 'x'})
    check('update missing client 404', r.status_code == 404, r.text)
    check('list clients', [x['id'] for x in c.get('/api/clients').json()['clients']] == ['claude'])
    r = c.request('DELETE', '/api/clients/claude', headers=J, content='{}')
    check('delete client', r.status_code == 200 and clients.load_clients() == [], r.text)

    # ---- アカウントの削除は .deleted へ移す ----
    r = c.request('DELETE', '/api/accounts/team-b', headers=J, content='{}')
    check('delete account moves file', r.status_code == 200 and r.json()['moved_to'].startswith('.deleted')
          and not (tmp / 'accounts' / 'team-b.toml').exists() and accounts.list_accounts() == ['team-a'], r.text)

    m = c.get('/api/manifest').json()['manifest']
    check('manifest served', '"chat:write.customize"' in m)

    c.post('/api/logout', headers=J, content='{}')
    c.cookies.clear()
    check('logged out', c.get('/api/clients').status_code == 401)


def main():
    fake_slack.BASE['url'] = f'http://127.0.0.1:{SLACK_PORT}'
    server = uvicorn.Server(uvicorn.Config(fake_slack.app, host='127.0.0.1', port=SLACK_PORT, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    try:
        run()
    finally:
        server.should_exit = True
        thread.join(5)
    print(f'\n{len(failures)} failures' if failures else '\nall passed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
