import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from app.main import Store
from app.manual import confirm, request


def test_jira_lookup_uses_project_and_pat():
    async def run():
        def handler(req):
            assert req.url.path == '/jira/rest/api/2/user/assignable/search'
            assert req.url.params['project'] == 'DEMO'
            assert req.headers['authorization'] == 'Bearer token'
            return httpx.Response(200, json=[{'name':'alex','displayName':'Alex'}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ctx=SimpleNamespace(application=SimpleNamespace(bot_data={'config':{'JIRA_AUTH_MODE':'pat','JIRA_TOKEN':'token','JIRA_BASE_URL':'https://jira.example/jira'},'http':client}))
            r=await request(ctx,'GET','/rest/api/2/user/assignable/search',params={'project':'DEMO','username':'al'})
            assert r.json()[0]['name']=='alex'
    asyncio.run(run())


def test_sprint_failure_does_not_recreate_issue(tmp_path):
    async def run():
        import time
        store=Store(str(tmp_path/'db'))
        payload={'type':'Task','summary':'Ship it','assignee':{'name':'alex','display':'Alex'},'sprint':{'id':123,'name':'Sprint 1'}}
        with store.db:
            cid=store.db.execute("INSERT INTO manual_candidates(chat_id,digest,payload,state,created) VALUES(?,?,?,'pending',?)",(-100,'abc',json.dumps(payload),int(time.time()))).lastrowid
        calls=[]
        def handler(req):
            calls.append(req)
            if req.url.path.endswith('/rest/api/2/issue'):
                assert json.loads(req.content)['fields']['assignee']=={'name':'alex'}
                return httpx.Response(201,json={'key':'DEMO-12'})
            return httpx.Response(403,json={'error':'forbidden'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c={'JIRA_AUTH_MODE':'pat','JIRA_TOKEN':'token','JIRA_BASE_URL':'https://jira.example','JIRA_PROJECT_KEY':'DEMO','groups':{-100},'approvers':{5},'expiry':24}
            q=SimpleNamespace(data=f'myes:{cid}',message=SimpleNamespace(chat_id=-100,reply_text=AsyncMock()),answer=AsyncMock(),edit_message_reply_markup=AsyncMock())
            update=SimpleNamespace(callback_query=q,effective_chat=SimpleNamespace(id=-100,type='group'),effective_user=SimpleNamespace(id=5))
            ctx=SimpleNamespace(application=SimpleNamespace(bot_data={'config':c,'http':client,'store':store}))
            await confirm(update,ctx)
            assert store.db.execute('select state,issue_key from manual_candidates').fetchone()==('created','DEMO-12')
            assert len(calls)==2 and 'sprint update failed' in q.message.reply_text.await_args.args[0]
            await confirm(update,ctx)
            assert len(calls)==2
    asyncio.run(run())


def test_group_wide_access_is_scoped_to_authorized_groups():
    from app.manual import allowed
    c={'groups':{-100}, 'approvers':set(), 'allow_all':True}
    context=SimpleNamespace(application=SimpleNamespace(bot_data={'config':c}))
    update=SimpleNamespace(effective_chat=SimpleNamespace(id=-100,type='group'),effective_user=SimpleNamespace(id=123))
    assert allowed(update,context)
    update.effective_chat.id=-200
    assert not allowed(update,context)
    update.effective_chat.id=-100
    c['allow_all']=False
    assert not allowed(update,context)
    c['approvers']={123}
    assert allowed(update,context)
