"""Guided, per-user Telegram issue creation without an AI service."""
import json
import logging
import re
import time
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, filters

log = logging.getLogger('bridge.manual')
TYPE, SUMMARY, ASSIGNEE_QUERY, ASSIGNEE_PICK, SPRINT_PICK = range(5)
TYPES = {'task': 'Task', 'bug': 'Bug', 'story': 'Story', 'epic': 'Epic'}


def allowed(update, context):
    c = context.application.bot_data['config']
    return bool(update.effective_chat and update.effective_chat.type in ('group', 'supergroup') and update.effective_chat.id in c['groups'] and update.effective_user and (c.get('allow_all', False) or update.effective_user.id in c['approvers']))


def draft(update, context):
    # Telegram user_data is shared across chats; isolate drafts by chat and user.
    return context.chat_data.setdefault('drafts', {}).setdefault(update.effective_user.id, {})


def auth(c):
    if c['JIRA_AUTH_MODE'] == 'pat':
        return {'Authorization': 'Bearer ' + c['JIRA_TOKEN'], 'Accept': 'application/json'}, None
    return {'Accept': 'application/json'}, (c['JIRA_USERNAME'], c['JIRA_PASSWORD'])


async def request(context, method, path, **kwargs):
    c = context.application.bot_data['config']; headers, credentials = auth(c)
    response = await context.application.bot_data['http'].request(method, c['JIRA_BASE_URL'].rstrip('/') + path, headers=headers, auth=credentials, **kwargs)
    response.raise_for_status()
    return response


async def start(update, context):
    if not allowed(update, context): return ConversationHandler.END
    draft(update, context).clear()
    await update.effective_message.reply_text('What would you like to create? Send /task, /bug, /story, or /epic. Send /cancel to stop.')
    return TYPE


async def choose_type(update, context):
    if not allowed(update, context): return ConversationHandler.END
    command = update.effective_message.text.split()[0].split('@')[0].lstrip('/').lower()
    draft(update, context).clear()
    draft(update, context).update(chat_id=update.effective_chat.id, issue_type=TYPES[command])
    await update.effective_message.reply_text(f"Creating a {TYPES[command]}. Send the summary (up to 255 characters).")
    return SUMMARY


async def summary(update, context):
    text = update.effective_message.text.strip()
    if not allowed(update, context) or draft(update, context).get('chat_id') != update.effective_chat.id: return ConversationHandler.END
    if not text or len(text) > 255:
        await update.effective_message.reply_text('Please send a summary between 1 and 255 characters.'); return SUMMARY
    draft(update, context)['summary'] = text
    await update.effective_message.reply_text('Type part of the assignee’s Jira name or username to search. Send /unassigned to leave it unassigned.')
    return ASSIGNEE_QUERY


async def query_users(update, context):
    if not allowed(update, context) or draft(update, context).get('chat_id') != update.effective_chat.id: return ConversationHandler.END
    query = update.effective_message.text.strip()
    if not query or len(query) > 100:
        await update.effective_message.reply_text('Enter a name of 1–100 characters.'); return ASSIGNEE_QUERY
    c = context.application.bot_data['config']
    try:
        r = await request(context, 'GET', '/rest/api/2/user/assignable/search', params={'project': c['JIRA_PROJECT_KEY'], 'username': query, 'maxResults': 20})
        users = [u for u in r.json() if u.get('active', True) and u.get('name')]
    except Exception:
        log.exception('Jira assignee lookup failed')
        await update.effective_message.reply_text('Could not search Jira users. Check Jira access and permissions, then try again.'); return ASSIGNEE_QUERY
    if not users:
        await update.effective_message.reply_text('No assignable users found. Try another search or /unassigned.'); return ASSIGNEE_QUERY
    draft(update, context)['users'] = [{'name': str(u['name']), 'display': str(u.get('displayName', u['name']))} for u in users]
    options = '\n'.join(f"{i}. {u['display']} ({u['name']})" for i, u in enumerate(draft(update, context)['users'], 1))
    await update.effective_message.reply_text('Choose a number, or send /search to search again:\n' + options)
    return ASSIGNEE_PICK


async def search_again(update, context):
    await update.effective_message.reply_text('Type part of the assignee’s Jira name or username.')
    return ASSIGNEE_QUERY


async def pick_user(update, context):
    if not allowed(update, context) or draft(update, context).get('chat_id') != update.effective_chat.id: return ConversationHandler.END
    try:
        index = int(update.effective_message.text) - 1
        users = draft(update, context)['users']
        if index < 0 or index >= len(users): raise ValueError()
    except (ValueError, KeyError):
        await update.effective_message.reply_text('Send one of the listed numbers, or /search.'); return ASSIGNEE_PICK
    draft(update, context)['assignee'] = users[index]
    return await choose_sprint(update, context)


async def unassigned(update, context):
    if not allowed(update, context) or draft(update, context).get('chat_id') != update.effective_chat.id: return ConversationHandler.END
    draft(update, context)['assignee'] = None
    return await choose_sprint(update, context)


async def choose_sprint(update, context):
    c = context.application.bot_data['config']; board = c['board']
    if not board:
        await update.effective_message.reply_text('No Jira board is configured. The issue will have no sprint.'); return await preview(update, context)
    try:
        sprints=[]; offset=0
        while True:
            r=await request(context, 'GET', f'/rest/agile/1.0/board/{board}/sprint', params={'state':'active', 'startAt':offset, 'maxResults':50})
            data=r.json(); values=data.get('values', []); sprints.extend(values)
            if data.get('isLast', True) or not values: break
            offset += len(values)
            if offset >= 500: raise ValueError('Too many active sprints')
        draft(update, context)['sprints']=[{'id':int(s['id']), 'name':str(s['name'])} for s in sprints]
    except Exception:
        log.exception('Jira sprint lookup failed')
        await update.effective_message.reply_text('Could not read active sprints. Send /retry to try again, or /nosprint to continue without one.')
        return SPRINT_PICK
    if not sprints:
        await update.effective_message.reply_text('No active sprint was found on the configured board. Send /retry or /nosprint.')
    else:
        lines='\n'.join(f"{i}. {s['name']} (ID {s['id']})" for i,s in enumerate(draft(update, context)['sprints'],1))
        if len(sprints)==1:
            await update.effective_message.reply_text('Current sprint: '+lines+'\nSend /default to use it, a number to select, or /nosprint.')
        else:
            await update.effective_message.reply_text('Multiple active sprints found. Send a number to select one, or /nosprint:\n'+lines)
    return SPRINT_PICK


async def retry_sprint(update, context):
    if not allowed(update,context): return ConversationHandler.END
    return await choose_sprint(update,context)


async def sprint_pick(update, context):
    if not allowed(update,context) or draft(update, context).get('chat_id') != update.effective_chat.id: return ConversationHandler.END
    try:
        sprints=draft(update, context).get('sprints',[])
        if update.effective_message.text.split('@')[0] == '/default':
            if len(sprints)!=1: raise ValueError()
            index=0
        else: index=int(update.effective_message.text)-1
        if index < 0 or index >= len(sprints): raise ValueError()
        draft(update, context)['sprint']=sprints[index]
    except ValueError:
        await update.effective_message.reply_text('Choose a listed sprint number, or /nosprint.'); return SPRINT_PICK
    return await preview(update,context)


async def no_sprint(update, context):
    if not allowed(update,context): return ConversationHandler.END
    draft(update, context)['sprint']=None
    return await preview(update,context)


async def preview(update, context):
    c=context.application.bot_data['config']; store=context.application.bot_data['store']; d=draft(update, context)
    payload={'type':d['issue_type'],'summary':d['summary'],'assignee':d.get('assignee'),'sprint':d.get('sprint')}
    digest=__import__('hashlib').sha256((payload['type'].casefold()+'\0'+re.sub(r'\s+',' ',payload['summary'].casefold())).encode()).hexdigest()
    now=int(time.time())
    with store.db:
        existing=store.db.execute("SELECT 1 FROM manual_candidates WHERE chat_id=? AND digest=? AND created>? AND state IN ('pending','creating','created','uncertain')", (update.effective_chat.id,digest,now-c['retention']*86400)).fetchone()
        if existing:
            await update.effective_message.reply_text('A matching issue proposal already exists in this group. Check it before creating another.'); return ConversationHandler.END
        cur=store.db.execute("INSERT INTO manual_candidates(chat_id,digest,payload,state,created) VALUES(?,?,?,'pending',?)",(update.effective_chat.id,digest,json.dumps(payload),now)); cid=cur.lastrowid
    assignee=payload['assignee']['display']+' ('+payload['assignee']['name']+')' if payload['assignee'] else 'Unassigned'
    sprint=payload['sprint']['name'] if payload['sprint'] else 'None'
    text=f"Review before creation:\nType: {payload['type']}\nSummary: {payload['summary']}\nAssignee: {assignee}\nSprint: {sprint}\nProject: {c['JIRA_PROJECT_KEY']}\nExpires in {c['expiry']} hours."
    keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('Create Jira issue', callback_data=f'myes:{cid}'),InlineKeyboardButton('Dismiss',callback_data=f'mno:{cid}')]])
    await update.effective_message.reply_text(text,reply_markup=keyboard)
    draft(update, context).clear()
    return ConversationHandler.END


async def confirm(update, context):
    q=update.callback_query; c=context.application.bot_data['config']; db=context.application.bot_data['store'].db
    if not allowed(update,context): await q.answer('Not authorized.',show_alert=True); return
    m=re.fullmatch(r'm(yes|no):(\d+)',q.data or '')
    if not m: await q.answer('Invalid action.'); return
    action,cid=m.group(1),int(m.group(2)); now=int(time.time())
    with db:
        new_state='creating' if action=='yes' else 'dismissed'
        cur=db.execute("UPDATE manual_candidates SET state=? WHERE id=? AND chat_id=? AND state='pending' AND created>?",(new_state,cid,q.message.chat_id,now-c['expiry']*3600))
        row=db.execute('SELECT payload FROM manual_candidates WHERE id=?',(cid,)).fetchone() if cur.rowcount else None
        if row and action=='no': db.execute('UPDATE manual_candidates SET payload=NULL WHERE id=?',(cid,))
    if not row: await q.answer('Already handled or expired.',show_alert=True); return
    await q.answer('Creating issue…' if action=='yes' else 'Dismissed.')
    if action=='no': await q.edit_message_reply_markup(reply_markup=None); return
    data=json.loads(row[0]); fields={'project':{'key':c['JIRA_PROJECT_KEY']},'issuetype':{'name':data['type']},'summary':data['summary']}
    fields['assignee']={'name':data['assignee']['name']} if data['assignee'] else None
    try:
        r=await request(context,'POST','/rest/api/2/issue',json={'fields':fields})
        key=r.json()['key']
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*-\d+',key): raise ValueError('Invalid Jira key')
    except Exception:
        with db: db.execute("UPDATE manual_candidates SET state='uncertain',payload=NULL WHERE id=?",(cid,))
        log.exception('Jira creation uncertain (candidate=%s)',cid)
        await q.message.reply_text(f'Issue creation outcome is uncertain (reference {cid}). Check Jira before trying again.')
        await q.edit_message_reply_markup(reply_markup=None); return
    # Save the issue key before the separate sprint request; never recreate on sprint failure.
    with db: db.execute("UPDATE manual_candidates SET state='created',issue_key=?,payload=NULL WHERE id=?",(key,cid))
    url=c['JIRA_BASE_URL'].rstrip('/')+'/browse/'+quote(key,safe='')
    suffix=''
    if data['sprint']:
        try:
            await request(context,'POST',f"/rest/agile/1.0/sprint/{data['sprint']['id']}/issue",json={'issues':[key]})
            suffix=f"\nAdded to sprint: {data['sprint']['name']}"
        except Exception:
            log.exception('Sprint update failed for %s',key)
            suffix='\nThe sprint update failed or its outcome is uncertain. Check the issue in Jira and set its sprint manually if needed.'
    await q.message.reply_text(f'Created {key}: {url}'+suffix)
    await q.edit_message_reply_markup(reply_markup=None)


async def cancel(update,context):
    draft(update, context).clear(); await update.effective_message.reply_text('Cancelled.'); return ConversationHandler.END


async def myid(update, context):
    if not update.effective_chat or update.effective_chat.type not in ('group', 'supergroup') or not update.effective_user:
        return
    await update.effective_message.reply_text(
        f'Your Telegram user ID: {update.effective_user.id}\nGroup ID: {update.effective_chat.id}'
    )


def install(app):
    app.add_handler(CommandHandler('myid', myid))
    conv=ConversationHandler(entry_points=[CommandHandler('start',start),CommandHandler(list(TYPES),choose_type)], states={TYPE:[CommandHandler(list(TYPES),choose_type)],SUMMARY:[MessageHandler(filters.TEXT & ~filters.COMMAND,summary)],ASSIGNEE_QUERY:[CommandHandler('unassigned',unassigned),MessageHandler(filters.TEXT & ~filters.COMMAND,query_users)],ASSIGNEE_PICK:[CommandHandler('search',search_again),CommandHandler('unassigned',unassigned),MessageHandler(filters.TEXT & ~filters.COMMAND,pick_user)],SPRINT_PICK:[CommandHandler('default',sprint_pick),CommandHandler('retry',retry_sprint),CommandHandler('nosprint',no_sprint),MessageHandler(filters.TEXT & ~filters.COMMAND,sprint_pick)]},fallbacks=[CommandHandler('cancel',cancel),CommandHandler('start',start)],per_chat=True,per_user=True,allow_reentry=True,conversation_timeout=3600)
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(confirm,pattern=r'^m(?:yes|no):\d+$'))
