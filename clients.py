"""MCP の接続元 (AI ごとのコネクタ) の設定と、コネクタ URL のトークンの管理。

AI ごとに 1 つのコネクタ URL (/mcp/<トークン>) を発行し、URL のトークンでどの AI かを見分ける。
設定は clients.json (環境変数 SLACK_MCP_CLIENTS_FILE、既定はリポジトリ直下。Git 管理外・600) に持つ。
サーバーはファイルの更新を毎回確かめるので、追加・作り直しに再起動は要らない。

AI ごとの設定:
  id          英小文字・数字・ハイフン・アンダースコアで 32 文字まで (ログとツールの結果に出る)
  name        bot として投稿するときの表示名 (例 "Claude")
  icon_emoji  bot として投稿するときのアイコンの絵文字 (例 ":robot_face:")。icon_url と同時には使わない
  icon_url    bot として投稿するときのアイコン画像の URL
  post_as     "bot" (既定。各アカウントの bot_token で、name・アイコンを付けて投稿する) か "user" (本人として投稿する)
  allow_as_user  true なら、bot の AI でも本人としてしかできない操作 (Canvas・List の作成と編集、チャンネルの作成) を許す
  accounts    使ってよいアカウント名の一覧 (省略か空ならすべて)
  read_only   true なら読み取りだけ (送信・変更系のツールを断る)

使い方 (リポジトリ直下で):
  python clients.py list
  python clients.py add <id> --name Claude --icon-emoji :robot_face: [--post-as user] [--accounts a,b] [--read-only]
                                       [--token-file mcp_token]   (古い mcp_token のトークンを引き継ぐとき)
  python clients.py set <id> [--name ...] [--icon-emoji ...] [--icon-url ...] [--post-as bot|user] [--accounts ...]
                             [--read-only|--writable] [--allow-as-user|--no-allow-as-user]
  python clients.py url <id>          コネクタの URL を表示する
  python clients.py regenerate <id>   トークンを作り直す (前の URL はすぐ使えなくなる)
  python clients.py remove <id>
"""
from pathlib import Path
import argparse
import datetime
import hmac
import json
import os
import re
import secrets
import sys

CLIENTS_FILE = Path(os.environ.get('SLACK_MCP_CLIENTS_FILE', Path(__file__).resolve().parent / 'clients.json'))
PUBLIC_BASE = os.environ.get('SLACK_MCP_PUBLIC_URL', 'https://swirhen.tv/slack_mcp/mcp').rstrip('/')
ID_RE = re.compile(r'^[a-z0-9_-]{1,32}$')
TOKEN_RE = re.compile(r'^[0-9a-f]{64}$')
EMOJI_RE = re.compile(r'^:[a-z0-9_+\-\']+:$')
POST_AS = ('bot', 'user')

_cache: dict = {'key': None, 'clients': []}


def _normalize(raw: dict) -> dict | None:
    cid = str(raw.get('id', ''))
    token = str(raw.get('token', ''))
    if not ID_RE.fullmatch(cid) or not TOKEN_RE.fullmatch(token):
        return None
    post_as = raw.get('post_as', 'bot')
    return {
        'id': cid,
        'token': token,
        'name': str(raw.get('name') or cid),
        'icon_emoji': raw.get('icon_emoji') or None,
        'icon_url': raw.get('icon_url') or None,
        'post_as': post_as if post_as in POST_AS else 'bot',
        'allow_as_user': bool(raw.get('allow_as_user', False)),
        'accounts': [str(a) for a in raw.get('accounts') or []],
        'read_only': bool(raw.get('read_only', False)),
        'created_at': raw.get('created_at'),
    }


def load_clients() -> list[dict]:
    """設定されている AI の一覧 (壊れた項目は除く)。ファイルが変わったときだけ読み直す"""
    try:
        st = CLIENTS_FILE.stat()
    except FileNotFoundError:
        return []
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    if _cache['key'] != key:
        try:
            data = json.loads(CLIENTS_FILE.read_text(encoding='utf-8'))
            raw = data.get('clients', []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            raw = []
        _cache.update(key=key, clients=[c for c in (_normalize(r) for r in raw if isinstance(r, dict)) if c])
    return _cache['clients']


def find_by_token(token: str) -> dict | None:
    """URL のトークンに対応する AI を返す (なければ None)。比較は時間の差が出ない方法で全件行う"""
    found = None
    for c in load_clients():
        if hmac.compare_digest(c['token'], token):
            found = c
    return found


def find_by_id(cid: str) -> dict | None:
    return next((c for c in load_clients() if c['id'] == cid), None)


def connector_url(client: dict) -> str:
    return f'{PUBLIC_BASE}/{client["token"]}'


# ---- 書き込み (CLI から使う) ----
def _read_raw() -> list[dict]:
    try:
        data = json.loads(CLIENTS_FILE.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    return list(data.get('clients', []))


def _write_raw(clients: list[dict]) -> None:
    temporary = CLIENTS_FILE.with_name(CLIENTS_FILE.name + '.tmp')
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as f:
        json.dump({'clients': clients}, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(temporary, CLIENTS_FILE)


def _apply_options(entry: dict, args) -> None:
    if args.name is not None:
        entry['name'] = args.name
    if args.icon_emoji is not None:
        if args.icon_emoji and not EMOJI_RE.fullmatch(args.icon_emoji):
            sys.exit('--icon-emoji は :robot_face: のようにコロンで囲む')
        entry['icon_emoji'] = args.icon_emoji or None
        if args.icon_emoji:
            entry['icon_url'] = None
    if args.icon_url is not None:
        entry['icon_url'] = args.icon_url or None
        if args.icon_url:
            entry['icon_emoji'] = None
    if args.post_as is not None:
        entry['post_as'] = args.post_as
    if args.accounts is not None:
        entry['accounts'] = [a.strip() for a in args.accounts.split(',') if a.strip()]
    if args.read_only is not None:
        entry['read_only'] = args.read_only
    if args.allow_as_user is not None:
        entry['allow_as_user'] = args.allow_as_user


def _describe(c: dict) -> str:
    icon = c.get('icon_emoji') or (c.get('icon_url') and 'icon_url') or '-'
    flags = [c['post_as']]
    if c.get('read_only'):
        flags.append('read_only')
    if c.get('allow_as_user'):
        flags.append('allow_as_user')
    accounts = ','.join(c.get('accounts') or []) or 'すべて'
    return f'{c["id"]:<16} 表示名={c["name"]}  アイコン={icon}  {"/".join(flags)}  アカウント={accounts}'


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='slack-mcp の接続元 (AI ごとのコネクタ) の管理')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list', help='一覧')
    for name in ('add', 'set'):
        p = sub.add_parser(name)
        p.add_argument('id')
        p.add_argument('--name')
        p.add_argument('--icon-emoji')
        p.add_argument('--icon-url')
        p.add_argument('--post-as', choices=POST_AS)
        p.add_argument('--accounts', help='使ってよいアカウント名をカンマ区切りで (空文字ですべて)')
        p.add_argument('--read-only', dest='read_only', action='store_true', default=None)
        p.add_argument('--writable', dest='read_only', action='store_false')
        p.add_argument('--allow-as-user', dest='allow_as_user', action='store_true', default=None)
        p.add_argument('--no-allow-as-user', dest='allow_as_user', action='store_false')
        if name == 'add':
            p.add_argument('--token-file', help='このファイルのトークンを引き継ぐ (古い mcp_token からの移行用)')
    for name in ('url', 'regenerate', 'remove'):
        sub.add_parser(name).add_argument('id')
    args = parser.parse_args(argv)

    raw = _read_raw()
    index = {c.get('id'): i for i, c in enumerate(raw)}
    if args.command == 'list':
        clients = load_clients()
        if not clients:
            print('AI はまだ登録されていない (python clients.py add <id> --name ...)')
        for c in clients:
            print(_describe(c))
        return
    if not ID_RE.fullmatch(args.id):
        sys.exit('id は英小文字・数字・ハイフン・アンダースコアで 32 文字まで')
    if args.command == 'add':
        if args.id in index:
            sys.exit(f'{args.id} はすでにある (変えるなら set)')
        token = secrets.token_hex(32)
        if args.token_file:
            token = Path(args.token_file).read_text(encoding='ascii').strip()
            if not TOKEN_RE.fullmatch(token):
                sys.exit(f'{args.token_file} のトークンの形が違う')
            if any(c.get('token') == token for c in raw):
                sys.exit('そのトークンはほかの AI が使っている')
        entry = {'id': args.id, 'token': token, 'name': args.id, 'post_as': 'bot',
                 'created_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
        _apply_options(entry, args)
        raw.append(entry)
        _write_raw(raw)
        print(_describe(_normalize(entry) or entry))
        print(f'コネクタの URL: {PUBLIC_BASE}/{token}')
        return
    if args.id not in index:
        sys.exit(f'{args.id} は登録されていない (python clients.py list)')
    entry = raw[index[args.id]]
    if args.command == 'set':
        _apply_options(entry, args)
        _write_raw(raw)
        print(_describe(_normalize(entry) or entry))
    elif args.command == 'url':
        print(f'{PUBLIC_BASE}/{entry["token"]}')
    elif args.command == 'regenerate':
        entry['token'] = secrets.token_hex(32)
        _write_raw(raw)
        print(f'トークンを作り直した。新しいコネクタの URL: {PUBLIC_BASE}/{entry["token"]}')
    elif args.command == 'remove':
        raw.pop(index[args.id])
        _write_raw(raw)
        print(f'{args.id} を消した (その URL はすぐ使えなくなる)')


if __name__ == '__main__':
    main()
