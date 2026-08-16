# Telegram Alert Setup — Website-Managed Recipients

This version keeps the Telegram bot token securely on Render, but the people/groups who receive alerts are configured from the Nesso Safety website.

## Alert levels

- NORMAL: silent Telegram update for near miss, low battery, offline/restored messages.
- URGENT: audible Telegram alert for possible STF.
- CRITICAL: audible Telegram alert for possible FFH. If the incident remains unacknowledged, the backend sends another critical alert after the configured delay.

## 1. Create the bot

1. Open Telegram and open `@BotFather`.
2. Run `/newbot`.
3. Follow BotFather's instructions and copy the bot token.
4. Keep the token private. Do not put it in GitHub, JavaScript, Arduino, or Supabase.

## 2. Add the token to Render

Render -> `dep-nesso-safety` -> Environment -> Add Environment Variable

- `TELEGRAM_BOT_TOKEN` = BotFather token
- `PUBLIC_DASHBOARD_URL` = `https://dep-nesso-safety.onrender.com` (optional, used for the Open Dashboard button)

You do NOT need `TELEGRAM_CHAT_ID` for the new website-managed setup.

## 3. Add recipients from the website

1. Open the Nesso Safety website.
2. Open `Telegram Alerts`.
3. Under `Who receives Telegram alerts?`, enter the admin password.
4. Enter a display name, such as `Site Supervisor` or `DEP Safety Group`.
5. Choose `Person` or `Telegram group`.
6. Choose the alert levels that this recipient should receive: Normal, Urgent, Critical.
7. Click `Connect Telegram recipient`.
8. The site creates a one-time pairing code valid for about 10 minutes.
9. Click `Open Telegram`.
   - For a person, press Start in the bot chat.
   - For a group, choose/add the bot to the target group. If Telegram does not pass the code automatically, send the `/link ...` command shown on the website inside the group.
10. Telegram replies that the recipient is connected.
11. Back on the website, click `Check connection` or `Load recipients`.

## 4. Manage who receives what

Every connected recipient has independent switches:

- Active
- Normal
- Urgent
- Critical

Examples:

- Project member: Normal + Urgent + Critical
- Site supervisor: Urgent + Critical only
- Emergency safety group: Critical only

Click `Save` after changing a recipient. Click `Remove` to stop sending alerts to that Telegram chat.

## 5. Test it

Use the test buttons on the Telegram Alerts page:

- Send normal test -> silent
- Send urgent test -> audible
- Send critical test -> audible critical-style message

Each recipient also has a `Test` button that sends a normal test only to that selected destination.

## Security design

- Telegram bot token: Render environment variable only.
- Recipient chat IDs: learned automatically when Telegram pairing completes, stored in Supabase, and never displayed in full in the browser.
- Recipient management endpoints require the dashboard `ADMIN_PASSWORD`.
- The public dashboard cannot add/remove recipients without the admin password.
