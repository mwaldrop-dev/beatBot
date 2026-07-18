# 🎺 Band Newsletter Bot

A Slack bot that monitors your school band program's Gmail inbox for newsletters from Membership Toolkit, indexes them for semantic search, announces new newsletters in Slack, and answers natural-language questions like *"What is call time for the game this week?"*

---

## Architecture

```
Gmail (new newsletter email)
  └─ Extract Membership Toolkit URL
       └─ Fetch public newsletter page
            └─ Parse & chunk text
                 ├─ Store in ChromaDB (semantic search, Gemini embeddings)
                 ├─ Record in SQLite (dedup tracking)
                 └─ Announce in Slack → #band-news

Band calendar (public Google Calendar iCal feeds)
  └─ Fetch + parse events every poll cycle
       └─ Upsert into the same ChromaDB collection (by event UID)

Slack user asks a question
  └─ Embed the question (Gemini)
       └─ Retrieve top matching chunks from ChromaDB (newsletters + calendar)
            └─ Gemini generates answer
                 └─ Reply in Slack with sources
```

---

## Setup — Step by Step

### 1. Google Cloud Console (Gmail API)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (e.g., "Band Newsletter Bot")
3. Enable the **Gmail API**: APIs & Services → Enable APIs → search "Gmail API" → Enable
4. Create OAuth2 credentials:
   - APIs & Services → Credentials → Create Credentials → **OAuth client ID**
   - Application type: **Desktop app**
   - Name: "Band Newsletter Bot"
   - Download the credentials (you'll need `client_id` and `client_secret`)
5. Add your Gmail address as a **Test user** under OAuth consent screen → Test users

### 2. Get a Gmail Refresh Token (run once, locally)

```bash
pip install google-auth-oauthlib
python scripts/gmail_auth.py
```

This opens a browser, you log in with the Gmail account that receives the newsletters, and it prints your refresh token. Copy all three values (`GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`).

### 3. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch
2. Name it "Band Newsletter Bot", pick your workspace

**Enable Socket Mode:**
- Settings → Socket Mode → Enable Socket Mode
- Create an App-Level Token with scope `connections:write` → Copy the `xapp-...` token → `SLACK_APP_TOKEN`

**Add Bot Token Scopes** (OAuth & Permissions → Scopes → Bot Token Scopes):
- `app_mentions:read`
- `chat:write`
- `im:history`
- `im:read`
- `im:write`
- `channels:history` *(optional, if you want the bot to read channel messages)*

**Enable Events:**
- Event Subscriptions → Enable Events
- Subscribe to bot events:
  - `app_mention`
  - `message.im`

**Install the app** to your workspace:
- OAuth & Permissions → Install to Workspace
- Copy the **Bot User OAuth Token** (`xoxb-...`) → `SLACK_BOT_TOKEN`

**Get your announcement channel ID:**
- In Slack, right-click the channel → View channel details → scroll to bottom → Copy channel ID (`C0XXXXXXXXX`) → `SLACK_ANNOUNCE_CHANNEL`
- Make sure to **invite the bot** to that channel: `/invite @BandNewsletterBot`

### 4. Gemini API Key

- [aistudio.google.com](https://aistudio.google.com) → Get API key → Create API key → `GEMINI_API_KEY`
- The bot uses `gemini-embedding-001` for embeddings and `gemini-flash-latest` for Q&A.
- Estimated cost: under $1/month for typical newsletter volume.

### 5. Band Calendar (optional)

If your program publishes a public Google Calendar (e.g. embedded on a Membership Toolkit page), the bot can pull events into its answers too:

1. Find the embedded calendar's `src` value: open the page with the calendar, view page source, and search for `calendar.google.com/calendar/embed?src=`. The value after `src=` (URL-decode the `%40` back to `@`) is the calendar ID, like `abc123...@group.calendar.google.com`.
2. Build the public iCal feed URL: `https://calendar.google.com/calendar/ical/<calendar-id>/public/basic.ics`
3. If there are multiple calendars (e.g. marching band, concert band, guard), repeat for each and join them with commas in `CALENDAR_ICAL_URLS`.
4. Optionally set `CALENDAR_INFO_URL` to the page where people can view the calendar themselves (used as the source link in Slack), and `CALENDAR_TIMEZONE` if your program isn't in `America/New_York`.

This only works for calendars set to "public" sharing — if the feed returns an error, the calendar owner needs to enable public access in the Google Calendar's sharing settings.

### 6. Deploy on Railway

1. Push this repo to GitHub
2. In Railway: **New Project** → Deploy from GitHub repo → select this repo
3. **Add a Volume** for persistent storage:
   - Your service → Settings → Volumes → Add Volume
   - Mount path: `/data`
4. **Set environment variables** (Railway → Variables):

| Variable | Value |
|---|---|
| `GMAIL_CLIENT_ID` | from step 2 |
| `GMAIL_CLIENT_SECRET` | from step 2 |
| `GMAIL_REFRESH_TOKEN` | from step 2 |
| `NEWSLETTER_SENDER_EMAIL` | e.g. `band@yourschool.org` |
| `NEWSLETTER_SUBJECT_KEYWORD` | e.g. `Newsletter` (or leave blank) |
| `SLACK_BOT_TOKEN` | `xoxb-...` from step 3 |
| `SLACK_APP_TOKEN` | `xapp-...` from step 3 |
| `SLACK_ANNOUNCE_CHANNEL` | `C0XXXXXXXXX` from step 3 |
| `GEMINI_API_KEY` | from step 4 |
| `CALENDAR_ICAL_URLS` | from step 5 (optional, comma-separated) |
| `CALENDAR_INFO_URL` | from step 5 (optional) |
| `CALENDAR_TIMEZONE` | from step 5 (optional, defaults to `America/New_York`) |
| `DATA_DIR` | `/data` |

5. Deploy — Railway will install dependencies and start the bot automatically.

---

## Usage

### Newsletter announcements
When a new newsletter email arrives, the bot automatically posts to your announcement channel:
> 📣 **New Band Newsletter!**
> **Marching Band Update — Week of Oct 14**
> [Read the full newsletter](https://yourschool.membershiptoolkit.com/...)
> *You can ask me questions about it — just mention me in this channel or send me a DM!*

### Q&A in a channel
Mention the bot anywhere:
> `@BandBot what is call time for Friday's game?`

In `SLACK_ANNOUNCE_CHANNEL` specifically, no mention is needed — just ask a question-like message directly and the bot replies in a thread.

### Q&A via direct message
Just message the bot directly — no mention needed.

### Help
Send `help` or `?` to the bot.

---

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in your env vars
cp .env.example .env
# edit .env

# Run locally (uses ./data/ for storage)
python main.py
```

---

## Backfilling the Newsletter Archive

Before starting the bot for the first time against an inbox that already has newsletters in it, run:

```bash
python scripts/backfill.py
```

This indexes every matching newsletter into the vector store (so Q&A works on the full history) and marks them processed, **without** posting a Slack announcement for each one. The normal scheduler will then skip everything already indexed and only announce genuinely new newsletters going forward.

> **Note:** Gmail's API defaults to returning the most recent 500 messages matching your query. For very large archives you may need to adjust the `maxResults` value in `gmail_client.py`.
