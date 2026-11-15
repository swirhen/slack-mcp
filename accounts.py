"""Slack アカウント (ワークスペースごとのユーザートークン) の設定の読み込み。

アカウントごとに accounts/<アカウント名>.toml が 1 つ (Git 管理外。トークンを含む)。
ファイルが変わったら読み直すので、追加・変更に再起動は要らない。

環境変数
  SLACK_MCP_ACCOUNTS_DIR  設定ファイルの置き場所 (省略時はリポジトリ直下の accounts/)
"""
from pathlib import Path
import os
import re
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # 本番は Ubuntu 22.04 の Python 3.10
    import tomli as tomllib

ACCOUNTS_DIR = Path(os.environ.get('SLACK_MCP_ACCOUNTS_DIR', Path(__file__).resolve().parent / 'accounts'))
NAME_RE = re.compile(r'^[a-z0-9_-]{1,32}$')
TOKEN_RE = re.compile(r'^xoxp-[A-Za-z0-9-]+$')
BOT_TOKEN_RE = re.compile(r'^xoxb-[A-Za-z0-9-]+$')

_cache: dict[str, tuple[tuple, dict]] = {}


class AccountError(ValueError):
    pass


def account_path(name: str) -> Path:
    if not NAME_RE.fullmatch(name or ''):
        raise AccountError(f'アカウント名「{name}」の形が違う (英小文字・数字・ハイフン・アンダースコアで 32 文字まで)')
    return ACCOUNTS_DIR / f'{name}.toml'


def list_accounts() -> list[str]:
    """設定ファイルのあるアカウント名を名前順で返す"""
    try:
        names = [p.stem for p in ACCOUNTS_DIR.glob('*.toml') if NAME_RE.fullmatch(p.stem)]
    except OSError:
        return []
    return sorted(names)


def read_account(name: str) -> dict:
    """アカウントの設定を返す ({'name', 'user_token', 'bot_token', 'label', 'read_only'})。ファイルが変わったときだけ読み直す"""
    path = account_path(name)
    try:
        st = path.stat()
    except FileNotFoundError:
        raise AccountError(f'アカウント「{name}」の設定がない ({path})。slack_list_accounts で名前を確かめる') from None
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    cached = _cache.get(name)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(path, 'rb') as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise AccountError(f'アカウント「{name}」の設定を読めない ({e})') from e
    token = str(data.get('user_token', '')).strip()
    if not TOKEN_RE.fullmatch(token):
        raise AccountError(f'アカウント「{name}」の user_token が未設定か形が違う (xoxp- で始まるトークンを書く)')
    bot_token = str(data.get('bot_token', '') or '').strip()
    if bot_token and not BOT_TOKEN_RE.fullmatch(bot_token):
        raise AccountError(f'アカウント「{name}」の bot_token の形が違う (xoxb- で始まる Bot User OAuth Token を書く)')
    config = {
        'name': name,
        'user_token': token,
        'bot_token': bot_token or None,
        'label': str(data.get('label', '') or ''),
        'read_only': bool(data.get('read_only', False)),
    }
    _cache[name] = (key, config)
    return config
