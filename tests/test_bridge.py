import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from app.main import Store, create_issue, detect, fingerprint, on_callback


def test_dedup_claim_and_expiry(tmp_path):
    s=Store(str(tmp_path/'db'))
    cid=s.add(-100,1,' Fix   API ', 'Fix API','Details',30)
    assert cid and s.add(-100,2,'fix api','Other','Details',30) is None
    assert s.claim(cid,-101,24) is None
    assert s.claim(cid,-100,24)==('Fix API','Details')
    assert s.claim(cid,-100,24) is None
    s.finish(cid,'created','ABC-1')
    assert s.db.execute('select state,summary,issue_key from candidates').fetchone()==('created',None,'ABC-1')
    s.db.execute('update candidates set created=?',(int(time.time())-31*86400,)); s.db.commit()
    s.cleanup(30,24)
    assert s.db.execute('select count(*) from candidates').fetchone()[0]==0


def test_uncertain_survives_cleanup_and_blocks_duplicate(tmp_path):
    s=Store(str(tmp_path/'db')); cid=s.add(1,1,'task','s','d',30)
    s.claim(cid,1,24); s.finish(cid,'uncertain')
    s.db.execute('update candidates set created=?',(int(time.time())-31*86400,)); s.db.commit(); s.cleanup(30,24)
    assert s.db.execute('select state from candidates').fetchone()[0]=='uncertain'


def test_ai_and_jira_request():
    async def run():
        seen=[]
        def handler(request):
            seen.append(request)
            if 'openai.com' in str(request.url):
                return httpx.Response(200,json={'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'actionable':True,'confidence':0.9,'summary':'Fix it','description':'Fix the API'})}]}]})
            return httpx.Response(201,json={'key':'PROJ-123'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c={'OPENAI_API_KEY':'secret','JIRA_BASE_URL':'https://jira.example.com/jira','JIRA_PROJECT_KEY':'PROJ','JIRA_ISSUE_TYPE':'Task','JIRA_AUTH_MODE':'pat','JIRA_TOKEN':'token'}
            task=await detect('Please fix the API',c,client)
            assert task['actionable'] and task['summary']=='Fix it'
            assert await create_issue(c,task['summary'],task['description'],client)=='PROJ-123'
            assert seen[-1].url.path=='/jira/rest/api/2/issue'
            assert seen[-1].headers['authorization']=='Bearer token'
    asyncio.run(run())


def test_unauthorized_approver_cannot_create(tmp_path):
    async def run():
        s=Store(str(tmp_path/'db')); cid=s.add(-100,1,'task','s','d',30)
        q=SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(type='group'),chat_id=-100),from_user=SimpleNamespace(id=999),data=f'yes:{cid}',answer=AsyncMock())
        ctx=SimpleNamespace(application=SimpleNamespace(bot_data={'config':{'groups':{-100},'approvers':{1}},'store':s}))
        await on_callback(SimpleNamespace(callback_query=q),ctx)
        assert s.db.execute('select state from candidates').fetchone()[0]=='pending'
        q.answer.assert_awaited_once()
    asyncio.run(run())
