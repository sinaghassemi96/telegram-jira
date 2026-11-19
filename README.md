# Telegram → Jira guided issue creator

Create Jira issues from an authorized Telegram group without an AI key. The bot guides an authorized user through issue type, summary, an assignee searched from Jira, and an active sprint. It displays a final review; an authorized approver must confirm before anything is created. The previous optional AI message-detection flow remains available when an OpenAI key is configured.

## 1. Configure Jira

Use your Jira Data Center account and PAT. Give it access to the project and at least Browse Projects, Create Issues, Assign Issues, and access to the Scrum board; adding issues to sprints may require additional project/board permissions. Jira's own permission and field rules apply. The Jira account should be dedicated to the integration and have only necessary access.

Find your Jira URL by opening an issue. For `https://jira.example.com/jira/browse/PROJ-12`, the base URL is `https://jira.example.com/jira`. The project key is `PROJ`. Use HTTPS with a certificate trusted by the container. For a private CA, add the CA to a derived image's trust store.

To find the **board ID**, open the relevant Scrum board in Jira and inspect the browser address. It often contains a number after `boards/` or a `rapidView=` parameter. The exact address varies by Jira version. You can also ask your Jira administrator. This must be the numeric board ID, not a sprint ID. The account must be able to view that board. A board can contain several active sprints; in that case the bot asks you to choose. Without a configured board, the issue is created without a sprint.

The four commands use Jira issue type names `Task`, `Bug`, `Story`, and `Epic`. These must exist with those names in your project's issue type scheme. Jira configurations often require additional fields, especially **Epic Name** for epics. The included issue creation sends only project, type, summary, and assignee; if your project requires more fields, adjust `app/manual.py` for your Jira configuration before using that type. Check required fields with your Jira administrator or the [Data Center create metadata APIs](https://developer.atlassian.com/server/jira/platform/jira-rest-api-examples/). This integration uses Jira Data Center usernames (`name`) for assignees, and PAT bearer auth or Basic username/password. It does not implement Jira Cloud authentication or OAuth. Verify API behavior for your Jira version.

## 2. Configure Telegram

1. Create a bot with [BotFather](https://core.telegram.org/bots/features#botfather) and add it to the group.
2. Obtain the group's numeric chat ID and the numeric Telegram user IDs of the people allowed to use and approve the bot, using a trusted administrative method. Group IDs are normally negative. Usernames are not sufficient. Members in `APPROVER_USER_IDS` can both start and approve proposals in every allowed group.
3. Disable the bot's group privacy mode in BotFather, then remove and re-add the bot if needed. Telegram's privacy mode can otherwise prevent it from receiving the ordinary messages used for summaries and choices. Restrict group membership and explain the bot's access to participants.

## 3. Set up and run

Copy `.env.example` to `.env`. Fill in the following values (the examples are not real credentials):

```dotenv
TELEGRAM_BOT_TOKEN=your-bot-token
AUTHORIZED_GROUP_IDS=-1001234567890
APPROVER_USER_IDS=123456789
JIRA_BASE_URL=https://jira.example.com/jira
JIRA_PROJECT_KEY=PROJ
JIRA_ISSUE_TYPE=Task
JIRA_BOARD_ID=42
JIRA_AUTH_MODE=pat
JIRA_TOKEN=your-jira-personal-access-token
OPENAI_API_KEY=
```

Leave `OPENAI_API_KEY` empty for the guided flow. The `JIRA_ISSUE_TYPE` setting is used by the optional AI flow; manual commands choose their own type. Set file permissions with `chmod 600 .env`, and never commit or share this file. Docker administrators can read container environment variables, so use an appropriate secrets manager in production.

Run `docker compose up -d --build` and check `docker compose logs -f`. Exactly one instance may use the bot token and database. The bot uses long polling and requires that no Telegram webhook is active. It needs outbound HTTPS to Telegram and Jira, and to OpenAI only if AI detection is enabled. Data is persisted in the Docker named volume. Back up the database consistently, including WAL data, and protect and encrypt backups.

## 4. Use the bot

In the authorized group:

1. Send `/start`, then `/task`, `/bug`, `/story`, or `/epic`. You can also start directly with one of those four commands.
2. Send the summary.
3. Type part of a Jira name or username, then send the number of the correct result. Use `/search` to search again or `/unassigned` to leave it unassigned, if your Jira project allows it.
4. For one active sprint, send `/default` to use it. If several are active, send the number of the desired sprint. Use `/nosprint` to continue without one, or `/retry` if lookup failed.
5. Review all the details and press **Create Jira issue**, or **Dismiss**. The bot replies with the Jira issue link. Send `/cancel` to abandon a conversation.

Conversations time out after one hour. Drafts are held in memory until reset or restart and are lost on restart. The bot operates in groups only. The bot does not collect an issue description in the guided flow. Users may need to open Jira afterward to add details. If your instance requires a description, add a step and field before using the integration.

## Safety and troubleshooting

The bot compares normalized summaries and issue types within the same group and retention period. This is an exact-match safeguard, not semantic deduplication or a Jira-wide search. Different wording can still produce duplicates. Existing pending, created, in-progress, and uncertain proposals block an identical manual proposal within the window. Manual and AI proposals use separate deduplication records, so they can overlap. A dismissed or expired proposal can be proposed again.

Jira issue creation and sprint assignment are **separate requests**. If Jira creates an issue but adding it to the sprint fails or times out, the bot returns the issue link and asks you to check the sprint in Jira; it never creates another issue to retry the sprint. A crash after creation but before the sprint request or link can leave an issue without a sprint or Telegram reply. Check Jira before trying again. If creation itself times out, fails, or returns an unexpected response, the bot reports an uncertain outcome and does not retry; search Jira manually. Stuck in-progress records become uncertain on restart. All uncertain records require manual reconciliation. A failure sending the initial proposal can leave a pending record that blocks a matching proposal until expiry. Monitor logs and reconcile as needed.

If no assignees appear, check the service account's permissions, the target project, the assignee's eligibility, and whether your Jira version supports the Data Center `user/assignable/search` endpoint and username format. Only the first 20 search results are shown; search more specifically. If sprint lookup fails, check `JIRA_BOARD_ID`, board visibility and Jira Software's Agile REST API. If issue creation fails, verify the issue type, required fields, account permissions, and Jira logs. Jira errors are intentionally not shown in the group because they may contain sensitive information.

SQLite stores group IDs, timestamps, normalized-summary hashes, candidate state, pending proposal data, and created issue keys. Pending data is cleared on completion, dismissal, expiration or uncertain outcome; terminal records are cleaned after `RETENTION_DAYS`, while uncertain/in-progress records are retained for manual reconciliation. Cleanup runs hourly, on startup, and on eligible AI messages. Hashes of predictable summaries can be guessed; SQLite deletion is not secure erasure. Protect the volume, WAL files, snapshots, backups, and logs. Jira and Telegram retain their own copies under their respective policies.

When `OPENAI_API_KEY` is set, the prior AI flow automatically sends eligible group text to OpenAI for task detection and asks for confirmation. The AI flow creates the configured `JIRA_ISSUE_TYPE`, and does not use the manual assignee or sprint steps. Review [OpenAI API data controls](https://platform.openai.com/docs/guides/your-data) and obtain organizational approval before enabling it. With no key, group messages outside the guided conversation are not sent to an LLM.

Run tests with `python -m pip install -r requirements.txt pytest` followed by `python -m pytest -q`. Tests mock Jira and do not replace an end-to-end test with your Jira, Telegram, board, permissions, and required fields. Set up monitoring, credential rotation, backups, dependency updates, and deployment controls before production.

## Temporary group-wide access and finding IDs

`ALLOW_ALL_GROUP_MEMBERS=true` allows every member of each authorized group to start, dismiss, and approve issues, including AI proposals. The Jira issue is still created only after someone confirms. Send `/myid` in a group to display your user ID and that group's ID. This read-only command works in groups even before the group is authorized.

To restrict access again, put your numeric ID in `APPROVER_USER_IDS`, set `ALLOW_ALL_GROUP_MEMBERS=false`, and restart with `docker compose up -d`. Check `AUTHORIZED_GROUP_IDS` contains only the intended group IDs. Do not share bot or Jira credentials.
