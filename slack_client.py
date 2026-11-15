"""Slack Web API の薄いクライアント (httpx)。

slack_sdk は使わず、メソッド名をそのまま渡して呼ぶ (Canvas・List など新しいメソッドもそのまま使えるように)。
引数は POST のフォームで送り、配列や辞書は JSON 文字列にする (slack_sdk と同じやり方で、Slack が受け付ける)。
レート制限 (HTTP 429 / error=ratelimited) は Retry-After に従って数回まで待って再試行する。

環境変数
  SLACK_API_BASE  Slack API の URL (省略時は https://slack.com/api。テストで偽のサーバーに向けるため)
"""
import asyncio
import json
import logging
import os
from typing import Any

import httpx

API_BASE = os.environ.get('SLACK_API_BASE', 'https://slack.com/api').rstrip('/')
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
MAX_RETRIES = 3
MAX_RETRY_WAIT = 30

log = logging.getLogger('slack-mcp.api')


class SlackError(Exception):
    """Slack API が ok=false を返したか、呼び出し自体に失敗した"""

    def __init__(self, error: str, detail: str | None = None, response: dict | None = None):
        self.error = error
        self.detail = detail
        self.response = response or {}
        super().__init__(f'{error}: {detail}' if detail else error)


_client: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
    return _client


def _encode(params: dict[str, Any]) -> dict[str, str]:
    out = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = 'true' if value else 'false'
        elif isinstance(value, (dict, list, tuple)):
            out[key] = json.dumps(value, ensure_ascii=False)
        else:
            out[key] = str(value)
    return out


async def call(token: str, method: str, **params: Any) -> dict:
    """Slack API のメソッドを呼んで、ok=true のレスポンスを返す。失敗は SlackError"""
    data = _encode(params)
    headers = {'Authorization': f'Bearer {token}'}
    url = f'{API_BASE}/{method}'
    for attempt in range(MAX_RETRIES + 1):
        try:
            res = await http().post(url, data=data, headers=headers)
        except httpx.HTTPError as e:
            raise SlackError('network_error', f'Slack API に接続できない ({type(e).__name__}: {e})') from e
        if res.status_code == 429 or (res.status_code == 200 and _is_ratelimited(res)):
            wait = _retry_after(res)
            if attempt >= MAX_RETRIES:
                raise SlackError('ratelimited', f'Slack API のレート制限 ({method})。{wait} 秒ほど待ってからやり直す')
            log.warning('%s: ratelimited, retry in %ss', method, wait)
            await asyncio.sleep(wait)
            continue
        if res.status_code != 200:
            raise SlackError('http_error', f'Slack API が HTTP {res.status_code} を返した ({method})')
        try:
            body = res.json()
        except ValueError as e:
            raise SlackError('invalid_response', f'Slack API の応答が JSON ではない ({method})') from e
        if not body.get('ok'):
            error = body.get('error', 'unknown_error')
            notes = []
            if body.get('detail'):
                notes.append(str(body['detail']))
            if body.get('needed'):
                notes.append(f'必要なスコープ: {body["needed"]}')
            notes.extend(body.get('response_metadata', {}).get('messages') or [])
            raise SlackError(error, '; '.join(notes) or None, body)
        return body
    raise SlackError('ratelimited')  # ここには来ない


def _is_ratelimited(res: httpx.Response) -> bool:
    try:
        return res.json().get('error') == 'ratelimited'
    except ValueError:
        return False


def _retry_after(res: httpx.Response) -> int:
    try:
        return max(1, min(MAX_RETRY_WAIT, int(res.headers.get('Retry-After', '3'))))
    except ValueError:
        return 3


async def download(token: str, url: str, max_bytes: int) -> tuple[bytes, str]:
    """files.info の url_private などをトークン付きで取得する。(本体, Content-Type) を返す"""
    try:
        async with http().stream('GET', url, headers={'Authorization': f'Bearer {token}'}) as res:
            if res.status_code != 200:
                raise SlackError('download_failed', f'ファイルの取得が HTTP {res.status_code} で失敗した')
            chunks = []
            size = 0
            async for chunk in res.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise SlackError('file_too_large', f'ファイルが {max_bytes // (1024 * 1024)} MB を超えるので取得しない')
                chunks.append(chunk)
            return b''.join(chunks), res.headers.get('content-type', '')
    except httpx.HTTPError as e:
        raise SlackError('network_error', f'ファイルを取得できない ({type(e).__name__}: {e})') from e


async def upload(url: str, data: bytes, content_type: str) -> None:
    """files.getUploadURLExternal で得た URL に本体を送る"""
    try:
        res = await http().post(url, content=data, headers={'Content-Type': content_type or 'application/octet-stream'})
    except httpx.HTTPError as e:
        raise SlackError('network_error', f'ファイルを送れない ({type(e).__name__}: {e})') from e
    if res.status_code != 200:
        raise SlackError('upload_failed', f'ファイルの送信が HTTP {res.status_code} で失敗した')


async def paginate(token: str, method: str, key: str, max_pages: int = 20, **params: Any):
    """cursor 方式のページを順にたどって、key の要素を yield する"""
    cursor = None
    for _ in range(max_pages):
        body = await call(token, method, cursor=cursor, **params)
        for item in body.get(key, []):
            yield item
        cursor = body.get('response_metadata', {}).get('next_cursor') or None
        if not cursor:
            return
