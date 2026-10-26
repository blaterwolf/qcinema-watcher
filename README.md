# QCinema Watcher

Checks these pages every 10 minutes and alerts you on **Discord** and by **email** when they change:

- https://qcinema.ph/schedule/
- https://qcinema.ph/festival-badge/
- plus any **brand-new page** published on qcinema.ph (in case they make a separate 2026 badge or schedule page)

It runs for free on GitHub Actions, so your laptop can be off.

**How it works:** qcinema.ph is a WordPress site, and WordPress publishes each page's "last edited" time and text at `qcinema.ph/wp-json/...`. The watcher compares that with what it saw last time and sends a message that shows what changed. It also calls out important words, for example **Now mentions "2026"** or **No longer says "SOLD OUT"**.

---

## Setup (about 10 minutes)

### 1. Make a Discord webhook

1. In Discord, use a server you own. To make one, click **+** in the server list, then **Create My Own**.
2. Open **Server Settings**, then **Integrations**, then **Webhooks**, then **New Webhook**.
3. Pick the channel the alerts should go to, then click **Copy Webhook URL**. Keep it private, because anyone with the URL can post in that channel.

### 2. Put these files on GitHub

1. Sign in at https://github.com (making an account is free).
2. Click **+** at the top right, then **New repository**. Name it `qcinema-watcher`, choose **Public**, and click **Create repository**.
   *Public makes the Actions minutes free and unlimited. Your Discord URL and passwords are stored as encrypted secrets, so nobody can see them. For a Private repo, see the notes at the bottom.*
3. On the new repo page, click **uploading an existing file**. Drag in **everything** from the unzipped folder: `watch.py`, `README.md` and the `.github` folder. Then click **Commit changes**.
4. Check that the file list shows `.github/workflows/watch.yml`. If the `.github` folder didn't upload, click **Add file**, then **Create new file**, type `.github/workflows/watch.yml` as the name, paste in the contents of that file, and commit.

### 3. Give it your Discord webhook

In the repo, go to **Settings**, then **Secrets and variables**, then **Actions**, then **New repository secret**:

| Name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | the webhook URL you copied |

### 4. Turn it on and test it

1. Open the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. Click **QCinema watcher** on the left, then **Run workflow**. Tick **Only send a test alert**, then click **Run workflow**.
3. Within about a minute you should get a test message in Discord and an email from GitHub.
4. Click **Run workflow** again, this time **without** the tick. You'll get a "QCinema Watcher is now running" message, and you're done. From now on it checks every 10 minutes by itself.

---

## Email

**You don't need to set anything up.** For each alert, the watcher opens an *issue* in your repo that tags you, which makes GitHub email it to the address on your GitHub account. It then closes the issue right away, so nothing piles up. Old alerts are kept under **Issues**, then **Closed**.
If no email arrives, go to https://github.com/settings/notifications and make sure **Email** is ticked under *Participating, @mentions and custom*. You can also choose which address GitHub sends to there.

**Optional: send the email from Gmail instead.** Add these repository secrets:

| Name | Value |
|---|---|
| `SMTP_USER` | your Gmail address |
| `SMTP_PASS` | a 16-character **app password** from https://myaccount.google.com/apppasswords (this needs 2-Step Verification turned on). Don't use your normal password. |
| `EMAIL_TO` | the address to send alerts to (for example your work email) |

To stop getting the GitHub emails as well, go to the **Variables** tab (next to Secrets) and add `ISSUE_ALERTS` = `false`.
For a mail server other than Gmail, also add the secrets `SMTP_HOST` and `SMTP_PORT`.

## Louder Discord pings (optional)

Under **Settings**, then **Secrets and variables**, then **Actions**, then the **Variables** tab, add `DISCORD_MENTION` = `@everyone`. Every alert will then ping everyone in the server, so your phone buzzes even when the channel is muted.

## Watching more pages

Open `watch.py` on GitHub, click the pencil icon, and copy one of the blocks in `WATCH`. The `slug` is the part of the address after `qcinema.ph/`, so for `https://qcinema.ph/qcshorts/` the slug is `qcshorts`.

## Good to know

- **Timing:** GitHub's scheduled runs are sometimes 5 to 15 minutes late when their servers are busy, and the very first scheduled run can take up to an hour to start.
- **If the site blocks GitHub:** if every check fails for an hour, you get one "can't reach qcinema.ph" alert, and another alert when it's working again.
- **Private repo instead of public:** free private repos get 2,000 Actions minutes a month, and checking every 10 minutes uses about 4,300. In `.github/workflows/watch.yml`, change `*/10` to `*/30` (every 30 minutes) to stay under the limit.
- **Auto-pause:** GitHub pauses scheduled workflows in public repos after 60 days with no activity. Re-enable it from the Actions tab if needed.
- **Stopping it:** open the **Actions** tab, click **QCinema watcher**, then **...**, then **Disable workflow**.
- `state.json` is the watcher's memory, and the watcher updates it automatically. Delete it to start fresh.
