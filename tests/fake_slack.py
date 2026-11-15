"""通し試験用の偽の Slack Web API (ASGI)。実際の Slack には接続しない。

2 つのワークスペース (トークン xoxp-test-a / xoxp-test-b) を持ち、主なメソッドをそれらしく返す。
受け取ったリクエストは calls に記録する (試験で引数を確かめるため)。
"""
import json
import time
from urllib.parse import parse_qs

TOKENS = {
    'xoxp-test-a': {'user_id': 'U0A1', 'user': 'alice', 'team_id': 'T0A', 'team': 'Team A', 'url': 'https://team-a.slack.com/'},
    'xoxp-test-b': {'user_id': 'U0B1', 'user': 'bob', 'team_id': 'T0B', 'team': 'Team B', 'url': 'https://team-b.slack.com/'},
}

USERS = {
    'U0A1': {'id': 'U0A1', 'name': 'alice', 'real_name': 'Alice Example', 'profile': {'display_name': 'alice', 'real_name': 'Alice Example', 'email': 'alice@example.com', 'title': 'Engineer'}, 'tz': 'Asia/Tokyo'},
    'U0A2': {'id': 'U0A2', 'name': 'taro', 'real_name': '山田 太郎', 'profile': {'display_name': 'taro', 'real_name': '山田 太郎', 'email': 'taro@example.com', 'title': '営業'}, 'tz': 'Asia/Tokyo'},
    'U0A3': {'id': 'U0A3', 'name': 'botty', 'real_name': 'Botty', 'is_bot': True, 'profile': {'display_name': '', 'real_name': 'Botty'}},
    'U0A4': {'id': 'U0A4', 'name': 'gone', 'real_name': 'Gone Person', 'deleted': True, 'profile': {'display_name': '', 'real_name': 'Gone Person'}},
    'U0B1': {'id': 'U0B1', 'name': 'bob', 'real_name': 'Bob Example', 'profile': {'display_name': 'bob', 'real_name': 'Bob Example', 'email': 'bob@example.org'}},
}

CHANNELS = {
    'C0A1': {'id': 'C0A1', 'name': 'general', 'is_channel': True, 'num_members': 3, 'topic': {'value': '全体連絡'}, 'purpose': {'value': ''}, 'created': 1700000000},
    'C0A2': {'id': 'C0A2', 'name': 'ops-alerts', 'is_channel': True, 'num_members': 2, 'topic': {'value': '障害の通知'}, 'purpose': {'value': 'ops'}, 'created': 1700000000},
    'C0A3': {'id': 'C0A3', 'name': 'old-project', 'is_channel': True, 'is_archived': True, 'num_members': 1, 'topic': {'value': ''}, 'purpose': {'value': ''}},
    'G0A1': {'id': 'G0A1', 'name': 'secret', 'is_group': True, 'is_private': True, 'num_members': 2, 'topic': {'value': ''}, 'purpose': {'value': 'ひみつ'}},
    'D0A1': {'id': 'D0A1', 'is_im': True, 'user': 'U0A2'},
    'C0B1': {'id': 'C0B1', 'name': 'general', 'is_channel': True, 'num_members': 1, 'topic': {'value': ''}, 'purpose': {'value': ''}},
}

MESSAGES = {
    'C0A1': [
        {'type': 'message', 'user': 'U0A2', 'text': 'こんにちは <@U0A1> さん、<#C0A2|ops-alerts> を見てください <https://example.com|リンク>', 'ts': '1700000100.000100',
         'thread_ts': '1700000100.000100', 'reply_count': 2, 'reply_users': ['U0A1'], 'latest_reply': '1700000300.000300',
         'reactions': [{'name': 'eyes', 'count': 2, 'users': ['U0A1', 'U0A2']}],
         'files': [{'id': 'F0A1', 'name': 'memo.txt', 'title': 'memo', 'mimetype': 'text/plain', 'filetype': 'text', 'size': 20}]},
        {'type': 'message', 'user': 'U0A1', 'text': 'おはよう &amp; よろしく', 'ts': '1700000050.000050'},
        {'type': 'message', 'subtype': 'bot_message', 'bot_id': 'B01', 'username': 'alertbot', 'text': 'alert!', 'ts': '1700000010.000010'},
    ],
    'D0A1': [{'type': 'message', 'user': 'U0A2', 'text': 'DM です', 'ts': '1700000500.000500'}],
    'C0B1': [{'type': 'message', 'user': 'U0B1', 'text': 'team b', 'ts': '1700000700.000700'}],
}
REPLIES = {
    ('C0A1', '1700000100.000100'): [
        MESSAGES['C0A1'][0],
        {'type': 'message', 'user': 'U0A1', 'text': '見ました', 'ts': '1700000200.000200', 'thread_ts': '1700000100.000100'},
        {'type': 'message', 'user': 'U0A1', 'text': '対応します', 'ts': '1700000300.000300', 'thread_ts': '1700000100.000100'},
    ],
}

FILES = {
    'F0A1': {'id': 'F0A1', 'name': 'memo.txt', 'title': 'memo', 'mimetype': 'text/plain', 'filetype': 'text', 'size': 20, 'user': 'U0A2',
             'created': 1700000100, 'permalink': 'https://team-a.slack.com/files/U0A2/F0A1/memo.txt', 'url_private': '{base}/files/F0A1'},
    'F0A2': {'id': 'F0A2', 'name': 'dot.png', 'title': 'dot', 'mimetype': 'image/png', 'filetype': 'png', 'size': 70, 'user': 'U0A1',
             'created': 1700000100, 'permalink': 'https://team-a.slack.com/files/U0A1/F0A2/dot.png', 'url_private': '{base}/files/F0A2'},
    'F0A3': {'id': 'F0A3', 'name': 'plan.pdf', 'title': 'plan', 'mimetype': 'application/pdf', 'filetype': 'pdf', 'size': 99999, 'user': 'U0A1',
             'created': 1700000100, 'permalink': 'https://team-a.slack.com/files/U0A1/F0A3/plan.pdf', 'url_private': '{base}/files/F0A3'},
    'FCANVAS': {'id': 'FCANVAS', 'title': 'Project Canvas', 'filetype': 'quip', 'mimetype': 'application/vnd.slack-docs', 'user': 'U0A1',
                'created': 1700000100, 'updated': 1700000200, 'permalink': 'https://team-a.slack.com/docs/T0A/FCANVAS', 'url_private': '{base}/files/FCANVAS'},
    'FLIST': {'id': 'FLIST', 'title': 'Tasks', 'filetype': 'list', 'user': 'U0A1', 'permalink': 'https://team-a.slack.com/lists/T0A/FLIST',
              'list_metadata': {'schema': [
                  {'id': 'Col1', 'key': 'task', 'name': 'Task', 'type': 'text', 'is_primary_column': True},
                  {'id': 'Col2', 'key': 'status', 'name': 'Status', 'type': 'select',
                   'options': {'format': 'single_select', 'choices': [{'value': 'opt_todo', 'label': 'To Do', 'color': 'blue'}, {'value': 'opt_done', 'label': 'Done', 'color': 'green'}]}},
                  {'id': 'Col3', 'key': 'owner', 'name': 'Owner', 'type': 'user'},
                  {'id': 'Col4', 'key': 'due', 'name': 'Due', 'type': 'date'},
                  {'id': 'Col5', 'key': 'done', 'name': 'Done?', 'type': 'checkbox'},
              ]}},
}
FILE_BODIES = {
    'F0A1': (b'hello from memo file', 'text/plain'),
    'F0A2': (bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0300050001'
                           '00efb6bb6b0000000049454e44ae426082'), 'image/png'),
    'F0A3': (b'%PDF-1.4 ...', 'application/pdf'),
    'FCANVAS': (b'<html><body><h1>Project Canvas</h1><p>Goal: ship it &amp; celebrate</p><ul><li>one</li><li>two</li></ul></body></html>', 'text/html'),
}
LIST_ITEMS = [
    {'id': 'Rec1', 'list_id': 'FLIST', 'fields': [
        {'column_id': 'Col1', 'key': 'task', 'rich_text': [{'type': 'rich_text', 'elements': [{'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': 'Write docs'}]}]}]},
        {'column_id': 'Col2', 'key': 'status', 'select': ['opt_todo']},
        {'column_id': 'Col3', 'key': 'owner', 'user': ['U0A2']},
        {'column_id': 'Col4', 'key': 'due', 'date': ['2026-12-01']},
        {'column_id': 'Col5', 'key': 'done', 'checkbox': False},
    ]},
]
EMOJI = {'partyparrot': 'https://emoji.example/partyparrot.gif', 'shipit': 'alias:squirrel', 'squirrel': 'https://emoji.example/squirrel.png'}

calls: list[tuple[str, dict]] = []
state = {'posted': [], 'scheduled': {}, 'reactions': {}, 'uploads': {}, 'canvas_edits': [], 'lists_created': [], 'list_updates': [], 'ratelimit_once': True}
BASE = {'url': ''}


def ok(**body):
    return 200, {'ok': True, **body}


def err(error, **extra):
    return 200, {'ok': False, 'error': error, **extra}


def paginated(items, params, key):
    limit = int(params.get('limit', 100))
    cursor = int(params.get('cursor') or 0)
    page = items[cursor:cursor + limit]
    nxt = str(cursor + limit) if cursor + limit < len(items) else ''
    return ok(**{key: page, 'response_metadata': {'next_cursor': nxt}})


def handle(method: str, params: dict, auth: dict):
    team = auth['team_id']
    if method == 'auth.test':
        return ok(**auth)
    if method == 'users.info':
        u = USERS.get(params.get('user'))
        return ok(user=u) if u else err('user_not_found')
    if method == 'users.list':
        members = [u for u in USERS.values() if (u['id'].startswith('U0A') if team == 'T0A' else u['id'].startswith('U0B'))]
        return paginated(members, params, 'members')
    if method == 'users.conversations':
        types = params.get('types', 'public_channel').split(',')
        out = []
        for c in CHANNELS.values():
            if (c['id'][1:3] == 'A' and team != 'T0A') or (c['id'][1:3] == 'B' and team != 'T0B'):
                pass
            if team == 'T0A' and c['id'].endswith('B1') or team == 'T0B' and not c['id'].endswith('B1'):
                continue
            kind = 'im' if c.get('is_im') else 'mpim' if c.get('is_mpim') else 'private_channel' if c.get('is_private') else 'public_channel'
            if kind not in types:
                continue
            if params.get('exclude_archived') == 'true' and c.get('is_archived'):
                continue
            out.append(c)
        return paginated(out, params, 'channels')
    if method == 'conversations.list':
        out = [c for c in CHANNELS.values() if not c.get('is_im') and (team == 'T0B') == c['id'].endswith('B1')]
        return paginated(out, params, 'channels')
    if method == 'conversations.info':
        c = CHANNELS.get(params.get('channel'))
        return ok(channel=c) if c else err('channel_not_found')
    if method == 'conversations.history':
        if state['ratelimit_once']:  # 1 回だけレート制限を返して再試行を確かめる
            state['ratelimit_once'] = False
            return 429, {'ok': False, 'error': 'ratelimited'}
        msgs = MESSAGES.get(params.get('channel'))
        if msgs is None:
            return err('channel_not_found')
        oldest = float(params.get('oldest') or 0)
        latest = float(params.get('latest') or 9e9)
        msgs = [m for m in msgs if oldest <= float(m['ts']) <= latest]
        return ok(messages=msgs[:int(params.get('limit', 100))], has_more=False, response_metadata={'next_cursor': ''})
    if method == 'conversations.replies':
        msgs = REPLIES.get((params.get('channel'), params.get('ts')))
        return ok(messages=msgs, has_more=False) if msgs else err('thread_not_found')
    if method == 'conversations.members':
        if params.get('channel') == 'C0A1':
            return ok(members=['U0A1', 'U0A2', 'U0A3', 'U0A4'], response_metadata={'next_cursor': ''})
        return err('channel_not_found')
    if method == 'conversations.open':
        users = params.get('users', '').split(',')
        if users == ['U0A2']:
            return ok(channel={'id': 'D0A1'})
        return ok(channel={'id': 'G0NEW'})
    if method == 'conversations.create':
        if params.get('name') == 'general':
            return err('name_taken')
        return ok(channel={'id': 'C0NEW', 'name': params.get('name'), 'is_private': params.get('is_private') == 'true'})
    if method == 'conversations.invite':
        return ok(channel={'id': params.get('channel')})
    if method == 'search.messages':
        q = params.get('query', '')
        matches = []
        for cid, msgs in MESSAGES.items():
            if (team == 'T0B') != cid.endswith('B1'):
                continue
            for m in msgs:
                if q.split()[0] in m.get('text', ''):
                    matches.append({**m, 'channel': {'id': cid, 'name': CHANNELS[cid].get('name'), 'is_im': CHANNELS[cid].get('is_im', False)},
                                    'permalink': f'https://team-a.slack.com/archives/{cid}/p{m["ts"].replace(".", "")}'})
        return ok(query=q, messages={'matches': matches, 'total': len(matches), 'pagination': {'total_count': len(matches), 'page': 1, 'page_count': 1}})
    if method == 'search.files':
        return ok(files={'matches': [FILES['F0A1'], FILES['FCANVAS']], 'total': 2, 'pagination': {'total_count': 2, 'page': 1, 'page_count': 1}})
    if method == 'chat.postMessage':
        if 'text' in params and 'markdown_text' in params:
            return err('markdown_text_conflict')
        if params.get('channel') == 'C0A3':
            return err('is_archived')
        ts = f'{time.time():.6f}'
        state['posted'].append({**params, 'ts': ts})
        return ok(channel=params['channel'], ts=ts, message={'text': params.get('markdown_text')})
    if method == 'chat.getPermalink':
        return ok(permalink=f'https://team-a.slack.com/archives/{params["channel"]}/p{params["message_ts"].replace(".", "")}')
    if method == 'chat.scheduleMessage':
        sid = f'Q{len(state["scheduled"]) + 1}'
        state['scheduled'][sid] = {'id': sid, 'channel_id': params['channel'], 'post_at': int(params['post_at']), 'text': params.get('markdown_text')}
        return ok(scheduled_message_id=sid, channel=params['channel'], post_at=int(params['post_at']))
    if method == 'chat.scheduledMessages.list':
        return ok(scheduled_messages=list(state['scheduled'].values()), response_metadata={'next_cursor': ''})
    if method == 'chat.deleteScheduledMessage':
        return ok() if state['scheduled'].pop(params.get('scheduled_message_id'), None) else err('invalid_scheduled_message_id')
    if method == 'reactions.add':
        key = (params['channel'], params['timestamp'], params['name'])
        if key in state['reactions']:
            return err('already_reacted')
        state['reactions'][key] = True
        return ok()
    if method == 'reactions.get':
        return ok(message={'reactions': [{'name': 'eyes', 'count': 2, 'users': ['U0A1', 'U0A2']}]}, channel=params['channel'])
    if method == 'files.info':
        f = FILES.get(params.get('file')) or state['uploads'].get(params.get('file'))
        if not f:
            return err('file_not_found')
        f = {k: v for k, v in f.items() if k not in ('body', 'length')}
        if f.get('url_private'):
            f['url_private'] = f['url_private'].format(base=BASE['url'])
        return ok(file=f)
    if method == 'files.getUploadURLExternal':
        fid = f'FUP{len(state["uploads"]) + 1}'
        state['uploads'][fid] = {'id': fid, 'name': params['filename'], 'title': params['filename'], 'size': int(params['length']),
                                 'length': int(params['length']), 'permalink': f'https://team-a.slack.com/files/U0A1/{fid}/{params["filename"]}', 'body': None}
        return ok(upload_url=f'{BASE["url"]}/upload/{fid}', file_id=fid)
    if method == 'files.completeUploadExternal':
        files = json.loads(params['files'])
        out = []
        for f in files:
            up = state['uploads'].get(f['id'])
            if not up or up['body'] is None:
                return err('file_not_found')
            if len(up['body']) != up['length']:
                return err('invalid_length')
            up['title'] = f.get('title') or up['title']
            up['shared'] = params.get('channel_id')
            up['comment'] = params.get('initial_comment')
            out.append({'id': f['id'], 'title': up['title']})
        return ok(files=out)
    if method == 'canvases.create':
        doc = json.loads(params['document_content'])
        assert doc['type'] == 'markdown'
        return ok(canvas_id='FNEWCANVAS')
    if method == 'canvases.edit':
        changes = json.loads(params['changes'])
        state['canvas_edits'].append(changes)
        if changes[0]['operation'] == 'delete' and changes[0].get('section_id') == 'missing':
            return err('canvas_editing_failed', detail='section not found')
        return ok()
    if method == 'canvases.sections.lookup':
        criteria = json.loads(params['criteria'])
        return ok(sections=[{'id': 'temp:C:sec1'}] if 'Goal' in criteria.get('contains_text', 'Goal') else [])
    if method == 'slackLists.create':
        schema = json.loads(params['schema'])
        state['lists_created'].append(params)
        meta = {'schema': [{**c, 'id': f'Col{i + 1}'} for i, c in enumerate(schema)]}
        return ok(list_id='FNEWLIST', list_metadata=meta)
    if method == 'slackLists.items.list':
        return ok(items=LIST_ITEMS, response_metadata={'next_cursor': ''})
    if method == 'slackLists.items.create':
        fields = json.loads(params['initial_fields'])
        state['list_updates'].append(('create', fields))
        return ok(item={'id': 'Rec2', 'list_id': params['list_id'], 'fields': fields})
    if method == 'slackLists.items.update':
        cells = json.loads(params['cells'])
        state['list_updates'].append(('update', cells))
        return ok()
    if method == 'slackLists.update':
        state['list_updates'].append(('meta', params))
        return ok()
    if method == 'emoji.list':
        return ok(emoji=EMOJI)
    return err('unknown_method')


async def app(scope, receive, send):
    if scope['type'] != 'http':
        return
    path = scope['path']
    body = b''
    while True:
        msg = await receive()
        body += msg.get('body', b'')
        if not msg.get('more_body'):
            break
    headers = {k.decode(): v.decode() for k, v in scope['headers']}
    if path.startswith('/files/'):
        data, ctype = FILE_BODIES[path.split('/')[-1]]
        return await respond(send, 200, data, ctype)
    if path.startswith('/upload/'):
        fid = path.split('/')[-1]
        state['uploads'][fid]['body'] = body
        return await respond(send, 200, b'OK', 'text/plain')
    method = path.split('/api/')[-1]
    token = headers.get('authorization', '').replace('Bearer ', '')
    params = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    calls.append((method, params))
    auth = TOKENS.get(token)
    if not auth:
        status, data = err('invalid_auth')
    else:
        status, data = handle(method, params, auth)
    out = json.dumps(data).encode()
    extra = [(b'retry-after', b'1')] if status == 429 else []
    await respond(send, status, out, 'application/json', extra)


async def respond(send, status, data, ctype, extra=()):
    await send({'type': 'http.response.start', 'status': status,
                'headers': [(b'content-type', ctype.encode()), (b'content-length', str(len(data)).encode()), *extra]})
    await send({'type': 'http.response.body', 'body': data})
