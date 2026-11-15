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
import threading

CLIENTS_FILE = Path(os.environ.get('SLACK_MCP_CLIENTS_FILE', Path(__file__).resolve().parent / 'clients.json'))
# コネクタの URL の前半 (例 https://example.com/slack_mcp/mcp)。空なら管理画面から見た相対パス ../mcp/<トークン> にし、
# 画面が開いている URL をもとに完全な URL に直す (管理画面を .../admin/、MCP を同じ階層の .../mcp/ で公開しているとき)
PUBLIC_BASE = os.environ.get('SLACK_MCP_PUBLIC_URL', '').rstrip('/')
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
    if PUBLIC_BASE:
        return f'{PUBLIC_BASE}/{client["token"]}'
    return f'../mcp/{client["token"]}'


def _cli_url(url: str) -> str:
    """コマンドで表示する URL。公開 URL が未設定のときは、相対パスであることを書き添える"""
    if PUBLIC_BASE:
        return url
    return f'{url}  (管理画面の URL から見た相対パス。SLACK_MCP_PUBLIC_URL を設定すると完全な URL を表示する)'


# ---- 書き込み (CLI と管理画面から使う。値が正しくなければ ValueError、ない id は KeyError) ----
_write_lock = threading.Lock()
OPTION_KEYS = ('name', 'icon_emoji', 'icon_url', 'post_as', 'accounts', 'read_only', 'allow_as_user')


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


def _apply_options(entry: dict, options: dict) -> None:
    """options の None でない項目を entry に書く (検証つき)"""
    unknown = set(options) - set(OPTION_KEYS)
    if unknown:
        raise ValueError(f'知らない項目: {", ".join(sorted(unknown))}')
    if options.get('name') is not None:
        name = str(options['name']).strip()
        if not name or len(name) > 80:
            raise ValueError('表示名は 1〜80 文字')
        entry['name'] = name
    if options.get('icon_emoji') is not None:
        emoji = str(options['icon_emoji']).strip()
        if emoji and not emoji.startswith(':'):
            emoji = f':{emoji.strip(":")}:'
        if emoji and not EMOJI_RE.fullmatch(emoji):
            raise ValueError('アイコンの絵文字は :robot_face: のような名前にする')
        entry['icon_emoji'] = emoji or None
        if emoji:
            entry['icon_url'] = None
    if options.get('icon_url') is not None:
        url = str(options['icon_url']).strip()
        if url and not re.fullmatch(r'https://\S+', url):
            raise ValueError('アイコンの URL は https:// で始める')
        entry['icon_url'] = url or None
        if url:
            entry['icon_emoji'] = None
    if options.get('post_as') is not None:
        if options['post_as'] not in POST_AS:
            raise ValueError('投稿方法は bot か user')
        entry['post_as'] = options['post_as']
    if options.get('accounts') is not None:
        accounts = options['accounts']
        if isinstance(accounts, str):
            accounts = accounts.split(',')
        entry['accounts'] = [str(a).strip() for a in accounts if str(a).strip()]
    for key in ('read_only', 'allow_as_user'):
        if options.get(key) is not None:
            entry[key] = bool(options[key])


def client_view(c: dict) -> dict:
    """管理画面・CLI に見せる形 (URL つき)"""
    out = _normalize(c) or dict(c)
    out['url'] = connector_url(out)
    return out


def create_client(cid: str, token: str | None = None, **options) -> dict:
    if not ID_RE.fullmatch(cid or ''):
        raise ValueError('id は英小文字・数字・ハイフン・アンダースコアで 32 文字まで')
    if token is not None and not TOKEN_RE.fullmatch(token):
        raise ValueError('引き継ぐトークンの形が違う (64 桁の 16 進数)')
    with _write_lock:
        raw = _read_raw()
        if any(c.get('id') == cid for c in raw):
            raise ValueError(f'{cid} はすでにある')
        token = token or secrets.token_hex(32)
        if any(c.get('token') == token for c in raw):
            raise ValueError('そのトークンはほかの AI が使っている')
        entry = {'id': cid, 'token': token, 'name': cid, 'post_as': 'bot',
                 'created_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
        _apply_options(entry, options)
        raw.append(entry)
        _write_raw(raw)
    return client_view(entry)


def _change(cid: str, change) -> dict:
    with _write_lock:
        raw = _read_raw()
        entry = next((c for c in raw if c.get('id') == cid), None)
        if entry is None:
            raise KeyError(cid)
        change(entry)
        _write_raw(raw)
    return client_view(entry)


def update_client(cid: str, **options) -> dict:
    return _change(cid, lambda entry: _apply_options(entry, options))


def regenerate_client(cid: str) -> dict:
    """トークンを作り直す。前の URL はすぐ使えなくなる"""
    return _change(cid, lambda entry: entry.update(token=secrets.token_hex(32)))


def remove_client(cid: str) -> None:
    with _write_lock:
        raw = _read_raw()
        rest = [c for c in raw if c.get('id') != cid]
        if len(rest) == len(raw):
            raise KeyError(cid)
        _write_raw(rest)


# ---- CLI ----
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

    if args.command == 'list':
        clients = load_clients()
        if not clients:
            print('AI はまだ登録されていない (python clients.py add <id> --name ...)')
        for c in clients:
            print(_describe(c))
        return
    options = {k: getattr(args, k, None) for k in OPTION_KEYS}
    try:
        if args.command == 'add':
            token = Path(args.token_file).read_text(encoding='ascii').strip() if args.token_file else None
            c = create_client(args.id, token=token, **options)
            print(_describe(c))
            print(f'コネクタの URL: {_cli_url(c["url"])}')
        elif args.command == 'set':
            print(_describe(update_client(args.id, **options)))
        elif args.command == 'url':
            c = find_by_id(args.id)
            if c is None:
                raise KeyError(args.id)
            print(_cli_url(connector_url(c)))
        elif args.command == 'regenerate':
            print(f'トークンを作り直した。新しいコネクタの URL: {_cli_url(regenerate_client(args.id)["url"])}')
        elif args.command == 'remove':
            remove_client(args.id)
            print(f'{args.id} を消した (その URL はすぐ使えなくなる)')
    except KeyError:
        sys.exit(f'{args.id} は登録されていない (python clients.py list)')
    except ValueError as e:
        sys.exit(str(e))


if __name__ == '__main__':
    main()
