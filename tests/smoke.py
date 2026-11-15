"""通し試験: 偽の Slack API と MCP サーバーを同じプロセスで立て、MCP クライアントからツールを呼ぶ。

実行 (リポジトリ直下で):  python tests/smoke.py
実際の Slack には接続しない。
"""
import asyncio
import base64
import json
import os
import pathlib
import sys
import tempfile
import time

import logging

import uvicorn

logging.basicConfig(level=logging.WARNING)

ROOT = pathlib.Path(__file__).resolve().parent.parent
SLACK_PORT = 18099
MCP_PORT = 18009
TOKEN = 'ab' * 32

tmp = pathlib.Path(tempfile.mkdtemp(prefix='slack-mcp-test-'))
(tmp / 'accounts').mkdir()
(tmp / 'accounts' / 'team-a.toml').write_text('user_token = "xoxp-test-a"\nlabel = "チーム A"\n', encoding='utf-8')
(tmp / 'accounts' / 'team-b.toml').write_text('user_token = "xoxp-test-b"\nread_only = true\n', encoding='utf-8')
(tmp / 'mcp_token').write_text(TOKEN + '\n', encoding='ascii')
os.environ['SLACK_API_BASE'] = f'http://127.0.0.1:{SLACK_PORT}/api'
os.environ['SLACK_MCP_ACCOUNTS_DIR'] = str(tmp / 'accounts')
os.environ['SLACK_MCP_TOKEN_FILE'] = str(tmp / 'mcp_token')
os.environ['SLACK_MCP_PORT'] = str(MCP_PORT)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'mcp_server'))
sys.path.insert(0, str(ROOT / 'tests'))
import fake_slack  # noqa: E402
import server  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.types import ImageContent, TextContent  # noqa: E402

fake_slack.BASE['url'] = f'http://127.0.0.1:{SLACK_PORT}'
failures = []


def check(name, cond, info=''):
    print(('  ok   ' if cond else '  FAIL ') + name + (f'  {info}' if info and not cond else ''))
    if not cond:
        failures.append(name)


async def call(session, tool_name, raw=False, **args):
    res = await session.call_tool(tool_name, args)
    if res.is_error:
        return {'error': res.content[0].text}
    if raw:
        return res.content
    if res.structured_content:
        return res.structured_content.get('result', res.structured_content)
    if len(res.content) == 1 and isinstance(res.content[0], TextContent):
        try:
            return json.loads(res.content[0].text)
        except ValueError:
            return res.content[0].text
    return res.content


async def run_tests():
    async with streamable_http_client(f'http://127.0.0.1:{MCP_PORT}/mcp/{TOKEN}') as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            check('29 tools listed', len(tools) == 29, len(tools))

            # アカウント
            r = await call(session, 'slack_list_accounts')
            names = {a['name']: a for a in r['accounts']}
            check('accounts: 2 accounts with team info', set(names) == {'team-a', 'team-b'} and names['team-a']['team'] == 'Team A' and names['team-a']['user_id'] == 'U0A1', r)
            check('accounts: read_only flag', names['team-b']['read_only'] is True)
            r = await call(session, 'slack_read_user_profile')
            check('account required when several', 'account' in r.get('error', ''), r)

            # チャンネル
            r = await call(session, 'slack_list_user_channels', account='team-a', types='public_channel,private_channel,im')
            kinds = {c['id']: c for c in r['channels']}
            check('list channels: public/private/im', kinds['C0A1']['type'] == 'public' and kinds['G0A1']['type'] == 'private' and kinds['D0A1']['type'] == 'im', r)
            check('list channels: im shows partner name', kinds['D0A1']['name'] == 'taro', kinds.get('D0A1'))
            check('list channels: topic', kinds['C0A2']['topic'] == '障害の通知')
            r = await call(session, 'slack_list_user_channels', account='team-a', exclude_archived=True)
            check('list channels: exclude archived', 'C0A3' not in {c['id'] for c in r['channels']})
            r = await call(session, 'slack_search_channels', account='team-a', query='障害')
            check('search channels by topic', [c['id'] for c in r['channels']] == ['C0A2'], r)
            r = await call(session, 'slack_search_channels', account='team-a', query='old', include_archived=True)
            check('search channels: archived flag', r['channels'] and r['channels'][0].get('archived') is True, r)
            r = await call(session, 'slack_list_channel_members', account='team-a', channel_id='C0A1')
            ids = [m['id'] for m in r['members']]
            check('members: bots and deleted filtered', ids == ['U0A1', 'U0A2'], r)
            r = await call(session, 'slack_list_channel_members', account='team-a', channel_id='C0A1', include_bots=True, include_deleted=True, response_format='concise')
            check('members: include all, concise', [m['id'] for m in r['members']] == ['U0A1', 'U0A2', 'U0A3', 'U0A4'] and r['members'][1]['name'] == 'taro', r)

            # メッセージ
            r = await call(session, 'slack_read_channel', account='team-a', channel_id='C0A1')
            msgs = r['messages']
            check('read channel: 3 messages, channel view', len(msgs) == 3 and r['channel']['name'] == 'general', r)
            check('read channel: retried after 429', not fake_slack.state['ratelimit_once'])
            m0 = msgs[0]
            check('read channel: mention/channel/link rendered', m0['text'] == 'こんにちは @alice さん、#ops-alerts を見てください リンク (https://example.com)', m0['text'])
            check('read channel: thread info', m0['thread']['reply_count'] == 2 and m0['thread']['reply_users'] == ['alice'], m0)
            check('read channel: reactions with users', m0['reactions'][0]['users'] == ['alice', 'taro'], m0)
            check('read channel: files', m0['files'][0]['id'] == 'F0A1')
            check('read channel: permalink', m0['permalink'] == 'https://team-a.slack.com/archives/C0A1/p1700000100000100', m0.get('permalink'))
            check('read channel: time in JST', m0['time'] == '2023-11-15T07:15:00+09:00', m0['time'])
            check('read channel: html unescape', msgs[1]['text'] == 'おはよう & よろしく', msgs[1]['text'])
            check('read channel: bot message', msgs[2]['bot']['name'] == 'alertbot' and msgs[2]['subtype'] == 'bot_message', msgs[2])
            r = await call(session, 'slack_read_channel', account='team-a', channel_id='U0A2', response_format='concise')
            check('read channel: user id -> DM', r['messages'][0]['text'] == 'DM です' and 'permalink' not in r['messages'][0], r)
            r = await call(session, 'slack_read_channel', account='team-a', channel_id='#general', oldest='2023-11-15T07:14:00', latest='1700000100.000100')
            check('read channel: #name and time range', [m['ts'] for m in r['messages']] == ['1700000100.000100', '1700000050.000050'], r)
            r = await call(session, 'slack_read_thread', account='team-a', channel_id='C0A1', message_ts='1700000100.000100')
            check('read thread: parent + replies', len(r['messages']) == 3 and r['messages'][1]['in_thread'] == '1700000100.000100', r)
            r = await call(session, 'slack_read_thread', account='team-a', channel_id='C0A1', message_ts='9.9')
            check('read thread: error hint', 'thread_not_found' in r.get('error', '') and '親メッセージ' in r['error'], r)
            r = await call(session, 'slack_search_messages', account='team-a', query='こんにちは in:#general')
            check('search messages', r['total'] == 1 and r['messages'][0]['channel']['name'] == 'general' and r['messages'][0]['user']['name'] == 'taro', r)
            r = await call(session, 'slack_search_messages', account='team-b', query='team')
            check('search messages: other account', r['total'] == 1 and r['messages'][0]['user']['name'] == 'bob', r)
            r = await call(session, 'slack_search_files', account='team-a', query='memo')
            check('search files', [f['id'] for f in r['files']] == ['F0A1', 'FCANVAS'] and r['files'][0]['user']['name'] == 'taro', r)

            # ユーザー
            r = await call(session, 'slack_search_users', account='team-a', query='太郎')
            check('search users by real name', [u['id'] for u in r['users']] == ['U0A2'] and r['users'][0]['email'] == 'taro@example.com' and r['my_user_id'] == 'U0A1', r)
            r = await call(session, 'slack_search_users', account='team-a', query='example.com')
            check('search users: bots/deleted excluded', [u['id'] for u in r['users']] == ['U0A1', 'U0A2'], r)
            r = await call(session, 'slack_read_user_profile', account='team-a')
            check('profile: self', r['id'] == 'U0A1' and r['tz'] == 'Asia/Tokyo', r)

            # 送信
            r = await call(session, 'slack_send_message', account='team-a', channel_id='C0A1', message='**hi** there', thread_ts='1700000100.000100', reply_broadcast=True)
            posted = fake_slack.state['posted'][-1]
            check('send: markdown_text + thread', posted.get('markdown_text') == '**hi** there' and posted.get('thread_ts') == '1700000100.000100'
                  and posted.get('reply_broadcast') == 'true' and 'text' not in posted, posted)
            check('send: permalink returned', r['permalink'].startswith('https://team-a.slack.com/archives/C0A1/p'), r)
            r = await call(session, 'slack_send_message', account='team-b', channel_id='C0B1', message='x')
            check('send: read_only account refused', 'read_only' in r.get('error', ''), r)
            r = await call(session, 'slack_send_message', account='team-a', channel_id='C0A3', message='x')
            check('send: archived hint', 'アーカイブ' in r.get('error', ''), r)
            r = await call(session, 'slack_send_message', account='team-a', channel_id='C0A1', message='x' * 12001)
            check('send: too long rejected locally', '長すぎる' in r.get('error', ''), r)
            r = await call(session, 'slack_schedule_message', account='team-a', channel_id='U0A2', message='later', post_at='2030-01-02T09:00')
            check('schedule: id and JST time', r['scheduled_message_id'] == 'Q1' and r['post_at'] == '2030-01-02T09:00:00+09:00' and r['channel_id'] == 'D0A1', r)
            r = await call(session, 'slack_schedule_message', account='team-a', channel_id='C0A1', message='later', post_at='2000-01-01T00:00')
            check('schedule: past rejected', '2 分以上先' in r.get('error', ''), r)
            r = await call(session, 'slack_list_scheduled_messages', account='team-a')
            check('schedule: list', [s['scheduled_message_id'] for s in r['scheduled_messages']] == ['Q1'], r)
            r = await call(session, 'slack_cancel_scheduled_message', account='team-a', channel_id='D0A1', scheduled_message_id='Q1')
            check('schedule: cancel', r.get('ok') is True and not fake_slack.state['scheduled'], r)

            # リアクション
            r = await call(session, 'slack_add_reaction', account='team-a', channel_id='C0A1', message_ts='1700000100.000100', emoji=':thumbsup:')
            check('reaction: add (colons stripped)', r.get('emoji') == 'thumbsup', r)
            r = await call(session, 'slack_add_reaction', account='team-a', channel_id='C0A1', message_ts='1700000100.000100', emoji='thumbsup')
            check('reaction: duplicate ok', r.get('ok') is True, r)
            r = await call(session, 'slack_get_reactions', account='team-a', channel_id='C0A1', message_ts='1700000100.000100')
            check('reaction: get with names', r['reactions'][0]['users'][1]['name'] == 'taro', r)

            # ファイル
            r = await call(session, 'slack_read_file', raw=True, account='team-a', file_id='F0A1')
            meta = json.loads(r[0].text)
            check('read file: text content', meta['content'] == 'hello from memo file' and meta['user']['name'] == 'taro', meta)
            r = await call(session, 'slack_read_file', raw=True, account='team-a', file_id='F0A2')
            check('read file: image returned', len(r) == 2 and isinstance(r[1], ImageContent) and r[1].mime_type == 'image/png', r)
            r = await call(session, 'slack_read_file', raw=True, account='team-a', file_id='F0A3')
            check('read file: binary -> note only', 'note' in json.loads(r[0].text) and len(r) == 1, r)
            r = await call(session, 'slack_read_file', account='team-a', file_id='FNONE')
            check('read file: not found hint', isinstance(r, dict) and 'file_not_found' in r['error'], r)
            r = await call(session, 'slack_upload_file', account='team-a', filename='notes.md', content='# hi\n', channel_id='C0A1', initial_comment='メモです')
            up = fake_slack.state['uploads']['FUP1']
            check('upload: text bytes, shared, comment', up['body'] == b'# hi\n' and up['shared'] == 'C0A1' and up['comment'] == 'メモです' and r['permalink'], (up, r))
            png = base64.b64encode(fake_slack.FILE_BODIES['F0A2'][0]).decode()
            r = await call(session, 'slack_upload_file', account='team-a', filename='dot.png', content_base64=png, title='A dot')
            check('upload: base64 private', fake_slack.state['uploads']['FUP2']['body'] == fake_slack.FILE_BODIES['F0A2'][0] and r['title'] == 'A dot' and 'shared_in' not in r, r)
            r = await call(session, 'slack_upload_file', account='team-a', filename='x', content='a', content_base64='YQ==')
            check('upload: both contents rejected', 'どちらか' in r.get('error', ''), r)

            # Canvas
            r = await call(session, 'slack_create_canvas', account='team-a', title='Plan', content='# Plan\n- a')
            check('canvas: create url', r['url'] == 'https://team-a.slack.com/docs/T0A/FNEWCANVAS', r)
            r = await call(session, 'slack_read_canvas', account='team-a', canvas_id='FCANVAS')
            check('canvas: read html -> text', 'Goal: ship it & celebrate' in r['content'] and r['title'] == 'Project Canvas', r)
            r = await call(session, 'slack_find_canvas_sections', account='team-a', canvas_id='FCANVAS', contains_text='Goal')
            check('canvas: sections', r['sections'] == [{'id': 'temp:C:sec1'}], r)
            r = await call(session, 'slack_update_canvas', account='team-a', canvas_id='FCANVAS', operation='insert_after', section_id='temp:C:sec1', content='new')
            edit = fake_slack.state['canvas_edits'][-1][0]
            check('canvas: edit change object', edit == {'operation': 'insert_after', 'section_id': 'temp:C:sec1', 'document_content': {'type': 'markdown', 'markdown': 'new'}}, edit)
            r = await call(session, 'slack_update_canvas', account='team-a', canvas_id='FCANVAS', operation='rename', title='Renamed')
            check('canvas: rename', fake_slack.state['canvas_edits'][-1][0]['title_content']['markdown'] == 'Renamed', r)
            r = await call(session, 'slack_update_canvas', account='team-a', canvas_id='FCANVAS', operation='insert_after', content='x')
            check('canvas: missing section_id rejected', 'section_id' in r.get('error', ''), r)
            r = await call(session, 'slack_update_canvas', account='team-a', canvas_id='FCANVAS', operation='delete', section_id='missing')
            check('canvas: api detail surfaced', 'section not found' in r.get('error', ''), r)

            # List
            r = await call(session, 'slack_create_list', account='team-a', name='Tracker', description='説明',
                           columns=[{'name': 'Task', 'type': 'text'}, {'name': 'Status', 'type': 'select', 'options': ['To Do', 'Done']},
                                    {'name': 'Tags', 'type': 'multi_select', 'options': ['a', 'b']}, {'name': 'Due', 'type': 'date'}])
            created = json.loads(fake_slack.state['lists_created'][-1]['schema'])
            check('list: create schema', created[0]['is_primary_column'] is True and created[1]['options']['format'] == 'single_select'
                  and created[2]['type'] == 'select' and created[2]['options']['format'] == 'multi_select' and created[1]['options']['choices'][0] == {'value': 'to_do', 'label': 'To Do', 'color': 'blue'}, created)
            check('list: create url and columns', r['url'] == 'https://team-a.slack.com/lists/T0A/FNEWLIST' and r['columns'][1]['options'] == ['To Do', 'Done'], r)
            r = await call(session, 'slack_create_list', account='team-a', name='NoText', columns=[{'name': 'N', 'type': 'number'}])
            check('list: primary text column prepended', json.loads(fake_slack.state['lists_created'][-1]['schema'])[0]['name'] == 'Task', r)
            r = await call(session, 'slack_read_list', account='team-a', list_id='FLIST')
            rec = r['records'][0]
            check('list: read records rendered', rec['Task'] == 'Write docs' and rec['Status'] == ['To Do'] and rec['Owner'][0]['name'] == 'taro'
                  and rec['Due'] == '2026-12-01' and rec['Done?'] is False and rec['record_id'] == 'Rec1', rec)
            r = await call(session, 'slack_read_list', account='team-a', list_id='FLIST', schema_only=True)
            check('list: schema only', 'records' not in r and len(r['columns']) == 5, r)
            r = await call(session, 'slack_add_list_record', account='team-a', list_id='FLIST', columns={'Task': 'New', 'status': 'done', 'Owner': 'U0A1', 'Due': '2026-12-31', 'Done?': 'yes'})
            fields = fake_slack.state['list_updates'][-1][1]
            by = {f['column_id']: f for f in fields}
            check('list: add record field mapping', by['Col1']['rich_text'][0]['elements'][0]['elements'][0]['text'] == 'New' and by['Col2']['select'] == ['opt_done']
                  and by['Col3']['user'] == ['U0A1'] and by['Col4']['date'] == ['2026-12-31'] and by['Col5']['checkbox'] is True and r['record_id'] == 'Rec2', fields)
            r = await call(session, 'slack_add_list_record', account='team-a', list_id='FLIST', columns={'Status': 'Nope'})
            check('list: bad option rejected', '選択肢' in r.get('error', ''), r)
            r = await call(session, 'slack_add_list_record', account='team-a', list_id='FLIST', columns={'Nothing': '1'})
            check('list: unknown column rejected', '列「Nothing」がない' in r.get('error', ''), r)
            r = await call(session, 'slack_update_list_record', account='team-a', list_id='FLIST', record_id='Rec1', updated_columns={'Status': 'Done'})
            cells = fake_slack.state['list_updates'][-1][1]
            check('list: update record cells', cells == [{'row_id': 'Rec1', 'column_id': 'Col2', 'select': ['opt_done']}], cells)
            r = await call(session, 'slack_update_list', account='team-a', list_id='FLIST', name='Renamed')
            check('list: update meta uses id', fake_slack.state['list_updates'][-1][1].get('id') == 'FLIST' and r.get('ok'), r)

            # 絵文字
            r = await call(session, 'slack_search_emojis', account='team-a', query='parrot, ship')
            check('emoji: search with alias', [e['name'] for e in r['emojis']] == ['partyparrot', 'shipit'] and r['emojis'][1]['alias_of'] == 'squirrel', r)

            # DM・チャンネル作成
            r = await call(session, 'slack_create_conversation', account='team-a', user_ids=['U0A2'])
            check('create conversation: DM', r['channel_id'] == 'D0A1' and r['type'] == 'im' and r['users'][0]['name'] == 'taro', r)
            r = await call(session, 'slack_create_conversation', account='team-a', channel_name='Project X!', user_ids=['U0A2'])
            check('create conversation: channel + invite', r['channel_id'] == 'C0NEW' and r['name'] == 'project-x' and r['invited'] == ['U0A2'], r)
            r = await call(session, 'slack_create_conversation', account='team-a', channel_name='general')
            check('create conversation: name_taken hint', 'すでにある' in r.get('error', ''), r)

            # 認証失敗のヒント
            (tmp / 'accounts' / 'team-c.toml').write_text('user_token = "xoxp-bad"\n', encoding='utf-8')
            r = await call(session, 'slack_read_user_profile', account='team-c')
            check('invalid token hint', 'invalid_auth' in r.get('error', '') and 'user_token' in r['error'], r)
            r = await call(session, 'slack_list_accounts')
            check('accounts: error shown per account', any(a['name'] == 'team-c' and 'error' in a for a in r['accounts']), r)

    # 間違ったコネクタ URL は 404
    import httpx
    async with httpx.AsyncClient() as c:
        res = await c.post(f'http://127.0.0.1:{MCP_PORT}/mcp/{"cd" * 32}', json={})
        check('wrong token -> 404', res.status_code == 404, res.status_code)
        res = await c.post(f'http://127.0.0.1:{MCP_PORT}/mcp', json={})
        check('no token -> 404', res.status_code == 404, res.status_code)


async def main():
    slack = uvicorn.Server(uvicorn.Config(fake_slack.app, host='127.0.0.1', port=SLACK_PORT, log_level='warning'))
    mcp = uvicorn.Server(uvicorn.Config(server.build_app(), host='127.0.0.1', port=MCP_PORT, log_level='warning', access_log=False))
    tasks = [asyncio.create_task(slack.serve()), asyncio.create_task(mcp.serve())]
    while not (slack.started and mcp.started):
        await asyncio.sleep(0.05)
    try:
        await run_tests()
    finally:
        slack.should_exit = mcp.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
    print(f'\n{len(failures)} failures' if failures else '\nall passed')
    return 1 if failures else 0


if __name__ == '__main__':
    start = time.time()
    code = asyncio.run(main())
    print(f'({time.time() - start:.1f}s)')
    sys.exit(code)
