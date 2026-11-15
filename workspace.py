"""アカウント (ワークスペース) ごとの状態と、Slack の応答を AI 向けに整える処理。

- auth.test の結果 (自分のユーザー ID・チーム・URL)、ユーザー情報、チャンネル一覧、ユーザー一覧をしばらく覚えておく
- メッセージの text の <@U…> <#C…|name> <url|label> などを読みやすい形にする
- ts を日本時間の ISO 8601 にする、パーマリンクを組み立てる
"""
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import asyncio
import datetime
import html
import os
import re
import time

import slack_client as api
from accounts import read_account
from slack_client import SlackError

try:
    TZ = ZoneInfo(os.environ.get('SLACK_MCP_TZ', 'Asia/Tokyo'))
except ZoneInfoNotFoundError:  # Windows でタイムゾーンのデータ (tzdata) がないとき
    TZ = datetime.timezone(datetime.timedelta(hours=9), 'JST')
AUTH_TTL = 3600
USER_TTL = 3600
USER_LIST_TTL = 600
CHANNEL_LIST_TTL = 300
PAGE = 200
MAX_LIST_PAGES = 50  # users.list / conversations.list を読む最大ページ数 (200 件 × 50 = 1 万件)
CONCURRENCY = 4

MENTION_RE = re.compile(r'<([^>]+)>')
USER_ID_RE = re.compile(r'<@([UW][A-Z0-9]+)')
CHANNEL_ID_RE = re.compile(r'^[CGD][A-Z0-9]+$')
USER_KEY_RE = re.compile(r'^[UW][A-Z0-9]+$')
TS_RE = re.compile(r'^\d{9,11}(\.\d{1,6})?$')


class Workspace:
    def __init__(self, name: str):
        self.name = name
        self._auth: tuple[float, dict] | None = None
        self._users: dict[str, tuple[float, dict | None]] = {}
        self._user_list: tuple[float, list[dict]] | None = None
        self._channel_lists: dict[str, tuple[float, list[dict]]] = {}
        self._semaphore = asyncio.Semaphore(CONCURRENCY)
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- 設定・認証 ----
    @property
    def config(self) -> dict:
        return read_account(self.name)

    @property
    def token(self) -> str:
        return self.config['user_token']

    @property
    def read_only(self) -> bool:
        return self.config['read_only']

    async def call(self, method: str, **params) -> dict:
        return await api.call(self.token, method, **params)

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def auth(self) -> dict:
        """auth.test の結果 ({'user_id', 'user', 'team_id', 'team', 'url'})。1 時間覚えておく"""
        if self._auth and self._auth[0] > time.time():
            return self._auth[1]
        async with self._lock('auth'):
            if self._auth and self._auth[0] > time.time():
                return self._auth[1]
            body = await self.call('auth.test')
            info = {'user_id': body.get('user_id'), 'user': body.get('user'), 'team_id': body.get('team_id'),
                    'team': body.get('team'), 'url': (body.get('url') or '').rstrip('/') + '/'}
            self._auth = (time.time() + AUTH_TTL, info)
            return info

    # ---- ユーザー ----
    async def user(self, user_id: str) -> dict | None:
        """users.info の user (見つからなければ None)。1 時間覚えておく"""
        cached = self._users.get(user_id)
        if cached and cached[0] > time.time():
            return cached[1]
        async with self._semaphore:
            cached = self._users.get(user_id)
            if cached and cached[0] > time.time():
                return cached[1]
            try:
                user = (await self.call('users.info', user=user_id)).get('user')
            except SlackError as e:
                if e.error in ('user_not_found', 'user_not_visible'):
                    user = None
                else:
                    raise
            self._users[user_id] = (time.time() + USER_TTL, user)
            return user

    async def users(self, user_ids) -> dict[str, dict | None]:
        ids = sorted({u for u in user_ids if u})
        results = await asyncio.gather(*(self.user(u) for u in ids))
        return dict(zip(ids, results))

    async def user_name(self, user_id: str | None) -> str | None:
        if not user_id:
            return None
        return user_label(await self.user(user_id)) or user_id

    async def all_users(self) -> list[dict]:
        """users.list の全員 (削除済み・ボットも含む)。10 分覚えておく"""
        if self._user_list and self._user_list[0] > time.time():
            return self._user_list[1]
        async with self._lock('users'):
            if self._user_list and self._user_list[0] > time.time():
                return self._user_list[1]
            members = [m async for m in api.paginate(self.token, 'users.list', 'members', max_pages=MAX_LIST_PAGES, limit=PAGE)]
            for m in members:
                self._users[m['id']] = (time.time() + USER_TTL, m)
            self._user_list = (time.time() + USER_LIST_TTL, members)
            return members

    # ---- チャンネル ----
    async def all_channels(self, types: str) -> list[dict]:
        """conversations.list の全チャンネル (アーカイブ済みも含む)。5 分覚えておく"""
        cached = self._channel_lists.get(types)
        if cached and cached[0] > time.time():
            return cached[1]
        async with self._lock('channels:' + types):
            cached = self._channel_lists.get(types)
            if cached and cached[0] > time.time():
                return cached[1]
            channels = [c async for c in api.paginate(self.token, 'conversations.list', 'channels',
                                                      max_pages=MAX_LIST_PAGES, types=types, limit=PAGE, exclude_archived=False)]
            self._channel_lists[types] = (time.time() + CHANNEL_LIST_TTL, channels)
            return channels

    async def resolve_channel(self, key: str) -> str:
        """チャンネル ID・ユーザー ID (その人との DM)・#名前 のどれでも受け取って、チャンネル ID にする"""
        key = (key or '').strip()
        if not key:
            raise SlackError('channel_not_found', 'channel_id が空')
        if CHANNEL_ID_RE.fullmatch(key):
            return key
        if USER_KEY_RE.fullmatch(key):
            body = await self.call('conversations.open', users=key)
            return body['channel']['id']
        name = key.lstrip('#').lower()
        for c in await self.all_channels('public_channel,private_channel'):
            if c.get('name', '').lower() == name:
                return c['id']
        raise SlackError('channel_not_found', f'「{key}」というチャンネルが見つからない')

    async def channel_info(self, channel_id: str) -> dict:
        try:
            return (await self.call('conversations.info', channel=channel_id)).get('channel', {})
        except SlackError:
            return {'id': channel_id}

    async def channel_view(self, c: dict) -> dict:
        """チャンネル (im / mpim も) を AI 向けの形にする"""
        if c.get('is_im'):
            other = c.get('user')
            return {'id': c['id'], 'type': 'im', 'name': await self.user_name(other), 'user_id': other,
                    'user_deleted': c.get('is_user_deleted', False)}
        kind = 'mpim' if c.get('is_mpim') else 'private' if c.get('is_private') else 'public'
        out = {'id': c['id'], 'type': kind, 'name': c.get('name')}
        if c.get('is_archived'):
            out['archived'] = True
        for key in ('topic', 'purpose'):
            value = (c.get(key) or {}).get('value')
            if value:
                out[key] = value
        if c.get('num_members') is not None:
            out['members'] = c['num_members']
        if c.get('is_ext_shared') or c.get('is_shared'):
            out['shared'] = 'slack_connect' if c.get('is_ext_shared') else 'internal'
        if c.get('created'):
            out['created'] = fmt_time(c['created'])
        return out

    # ---- メッセージ ----
    async def permalink(self, channel: str, ts: str, thread_ts: str | None = None) -> str:
        base = (await self.auth())['url']
        link = f'{base}archives/{channel}/p{ts.replace(".", "")}'
        if thread_ts and thread_ts != ts:
            link += f'?thread_ts={thread_ts}&cid={channel}'
        return link

    async def render_text(self, text: str | None, users: dict[str, dict | None] | None = None) -> str:
        """mrkdwn のメンション・リンクを読みやすくする"""
        if not text:
            return ''
        if users is None:
            users = await self.users(USER_ID_RE.findall(text))

        def repl(m):
            body = m.group(1)
            label = None
            if '|' in body:
                body, label = body.split('|', 1)
            if body.startswith('@'):
                uid = body[1:]
                return '@' + (user_label(users.get(uid)) or label or uid)
            if body.startswith('#'):
                return '#' + (label or body[1:])
            if body.startswith('!subteam^'):
                return label or '@group'
            if body.startswith('!date'):
                return label or body
            if body.startswith('!'):
                return '@' + body[1:]
            return f'{label} ({body})' if label and label != body else body

        return html.unescape(MENTION_RE.sub(repl, text))

    async def render_messages(self, messages: list[dict], channel: str, concise: bool = False) -> list[dict]:
        ids = set()
        for m in messages:
            if m.get('user'):
                ids.add(m['user'])
            ids.update(USER_ID_RE.findall(m.get('text') or ''))
            for r in m.get('reactions') or []:
                if not concise:
                    ids.update(r.get('users') or [])
        users = await self.users(ids)
        return [await self.render_message(m, channel, users, concise) for m in messages]

    async def render_message(self, m: dict, channel: str, users: dict[str, dict | None], concise: bool = False) -> dict:
        ts = m.get('ts', '')
        out: dict = {'ts': ts, 'time': fmt_ts(ts)}
        if m.get('user'):
            out['user'] = {'id': m['user'], 'name': user_label(users.get(m['user'])) or m.get('username') or m['user']}
        elif m.get('bot_id') or m.get('username'):
            out['bot'] = {'id': m.get('bot_id'), 'name': (m.get('bot_profile') or {}).get('name') or m.get('username')}
        text = await self.render_text(m.get('text'), users)
        out['text'] = text[:300] + '…' if concise and len(text) > 300 else text
        if m.get('subtype'):
            out['subtype'] = m['subtype']
        thread_ts = m.get('thread_ts')
        if thread_ts and thread_ts == ts and m.get('reply_count'):
            out['thread'] = {'reply_count': m['reply_count'], 'latest_reply': fmt_ts(m.get('latest_reply'))}
            if not concise:
                out['thread']['reply_users'] = [user_label(users.get(u)) or u for u in m.get('reply_users') or []]
        elif thread_ts and thread_ts != ts:
            out['in_thread'] = thread_ts
        if m.get('reactions'):
            out['reactions'] = [{'name': r.get('name'), 'count': r.get('count')} for r in m['reactions']] if concise else \
                [{'name': r.get('name'), 'count': r.get('count'), 'users': [user_label(users.get(u)) or u for u in r.get('users') or []]}
                 for r in m['reactions']]
        if m.get('files'):
            out['files'] = [file_summary(f) for f in m['files']]
        if m.get('attachments') and not concise:
            out['attachments'] = [attachment_summary(a) for a in m['attachments'] if attachment_summary(a)]
        if m.get('edited'):
            out['edited'] = True
        if not concise and ts:
            out['permalink'] = await self.permalink(channel, ts, thread_ts)
        return out


# ---- 単純な整形 ----
def user_label(user: dict | None) -> str | None:
    if not user:
        return None
    profile = user.get('profile') or {}
    return profile.get('display_name') or profile.get('real_name') or user.get('real_name') or user.get('name')


def user_view(user: dict, detailed: bool = False) -> dict:
    profile = user.get('profile') or {}
    out = {'id': user['id'], 'name': user.get('name'), 'real_name': user.get('real_name') or profile.get('real_name'),
           'display_name': profile.get('display_name') or None}
    for key in ('email', 'title'):
        if profile.get(key):
            out[key] = profile[key]
    if user.get('deleted'):
        out['deleted'] = True
    if user.get('is_bot') or user.get('is_app_user'):
        out['bot'] = True
    if user.get('is_admin'):
        out['admin'] = True
    if detailed:
        out.update({'tz': user.get('tz'), 'tz_label': user.get('tz_label'), 'phone': profile.get('phone') or None,
                    'status_text': profile.get('status_text') or None, 'status_emoji': profile.get('status_emoji') or None,
                    'owner': user.get('is_owner', False), 'restricted': user.get('is_restricted', False),
                    'team_id': user.get('team_id'), 'updated': fmt_time(user.get('updated'))})
        out = {k: v for k, v in out.items() if v is not None}
    return out


def file_summary(f: dict) -> dict:
    out = {'id': f.get('id'), 'name': f.get('name'), 'title': f.get('title'), 'mimetype': f.get('mimetype'),
           'filetype': f.get('filetype'), 'size': f.get('size')}
    if f.get('mode') == 'tombstone' or f.get('file_access') in ('access_denied', 'file_not_found'):
        out['unavailable'] = f.get('file_access') or 'deleted'
    if f.get('permalink'):
        out['permalink'] = f['permalink']
    return {k: v for k, v in out.items() if v is not None}


def attachment_summary(a: dict) -> dict | None:
    out = {}
    for key in ('title', 'author_name', 'pretext', 'text', 'fallback', 'title_link', 'from_url'):
        if a.get(key):
            out[key] = a[key][:500] if isinstance(a[key], str) else a[key]
    return out or None


def fmt_ts(ts: str | float | None) -> str | None:
    """Slack の ts ("1700000000.123456") を日本時間の ISO 8601 にする"""
    if ts in (None, ''):
        return None
    try:
        return datetime.datetime.fromtimestamp(float(ts), TZ).isoformat(timespec='seconds')
    except (TypeError, ValueError, OverflowError):
        return None


def fmt_time(seconds: int | float | None) -> str | None:
    return fmt_ts(seconds)


def parse_time(value: str | int | float | None, what: str = '時刻') -> float | None:
    """Unix 秒 (Slack の ts も可) か ISO 8601 (タイムゾーン省略時は日本時間) を Unix 秒にする"""
    if value in (None, ''):
        return None
    text = str(value).strip()
    if TS_RE.fullmatch(text):
        return float(text)
    try:
        dt = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(f'{what}「{value}」の形が違う (Unix 秒か、2026-01-02T15:04 のような ISO 8601)') from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.timestamp()


def slack_ts(value, what: str = '時刻') -> str | None:
    seconds = parse_time(value, what)
    return None if seconds is None else f'{seconds:.6f}'


def rich_text_block(text: str) -> dict:
    """List の説明やテキスト列に使う rich_text ブロック (プレーンテキスト)"""
    return {'type': 'rich_text', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': text}]}]}


def flatten_rich_text(blocks) -> str:
    """rich_text ブロックをプレーンテキストにする"""
    parts: list[str] = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            return
        t = node.get('type')
        if t == 'text':
            parts.append(node.get('text', ''))
        elif t == 'link':
            parts.append(node.get('text') or node.get('url', ''))
        elif t == 'user':
            parts.append(f'<@{node.get("user_id")}>')
        elif t == 'channel':
            parts.append(f'<#{node.get("channel_id")}>')
        elif t == 'emoji':
            parts.append(f':{node.get("name")}:')
        elif t == 'broadcast':
            parts.append(f'@{node.get("range")}')
        elif t == 'rich_text_list':
            for item in node.get('elements', []):
                parts.append('\n- ')
                walk(item)
            parts.append('\n')
        elif t in ('rich_text_section', 'rich_text_quote', 'rich_text_preformatted', 'rich_text'):
            walk(node.get('elements', []))
            if t != 'rich_text':
                parts.append('\n')
        elif 'elements' in node:
            walk(node['elements'])

    walk(blocks)
    return ''.join(parts).strip()


def html_to_text(text: str) -> str:
    text = re.sub(r'(?is)<(script|style).*?</\1>', '', text)
    text = re.sub(r'(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr)>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\n{3,}', '\n\n', html.unescape(text)).strip()
