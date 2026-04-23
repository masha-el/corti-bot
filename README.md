# Corti — Personal Assistant Telegram Bot

Corti is a Telegram bot that acts as a single interface for Gmail, Google Calendar, Notion, and Todoist.
Send text or voice messages in natural language — Corti figures out what you mean and gets it done.

---

## What it does

- 📅 **Google Calendar** — read, create, edit, and delete events
- 📧 **Gmail** — read emails, send, reply, forward, and delete
- 📄 **Notion** — search, read, and create notes under any parent page
- ✅ **Todoist** — read, create, complete, and delete tasks
- 🎙️ **Voice messages** — transcribed via Whisper and handled like text
- 🧠 **LLM intent routing** — no commands needed, just talk naturally

---

## Architecture

```
You (Telegram)
    ↓ text or voice message
Telegram Bot (python-telegram-bot)
    ↓ voice → Groq Whisper → transcript
    ↓ text
Intent Router (Groq LLaMA 3.3 70B)
    ↓ classifies intent + extracts params → JSON
Service Layer
    ├── Gmail API
    ├── Google Calendar API
    ├── Notion API
    └── Todoist API
    ↓ formatted result
Telegram → reply
```

The LLM does one job: read the message, decide which service to call, extract the parameters as JSON. Every other component is deterministic.

---

## Stack

| Component | Technology |
|---|---|
| Bot framework | python-telegram-bot v21 |
| LLM + STT | Groq (LLaMA 3.3 70B + Whisper large-v3) |
| Calendar + Gmail | Google API Python Client (OAuth2) |
| Notion | notion-client |
| Todoist | todoist-api-python v4 |
| Persistence | python-telegram-bot PicklePersistence |
| Containerization | Docker + Docker Compose |
| CI/CD | GitHub Actions → GHCR → GCP via IAP tunnel |
| Infrastructure | GCP e2-micro VM |

---

## Features

### Natural language understanding
```
"What's on my calendar this week?"
"Add standup tomorrow at 9am"
"Show my last 3 emails"
"Save a note under Job Search: interview prep checklist"
"Mark the PR review task as done"
```

### Shortcut commands
| Command | Action |
|---|---|
| `cal add` | Add calendar event |
| `cal edit` | Edit calendar event |
| `cal delete` | Delete calendar event |
| `gmail send` | Send an email |
| `noti write` | Create a Notion note |
| `noti search` | Search Notion |
| `todo add` | Add a Todoist task |
| `todo done` | Complete a task |
| `todo del` | Delete a task |

### Multi-step conversation flows
Incomplete commands trigger clarification prompts with cancel buttons at every step:
```
User:  cal edit
Corti: What's the name of the event?     [ ❌ Cancel ]
User:  standup
Corti: What date is it on?               [ ❌ Cancel ]
User:  tomorrow
Corti: Found: Standup — Thursday 10-04-2026 at 09:00
       What do you want to change?
       • title / date / time / duration  [ ❌ Cancel ]
```

### Email actions
Each email shows inline buttons:

```
📧 Subject line
From: sender@example.com
Email preview snippet...

[ ⬇️ Show more ]  [ Actions » ]
```

Actions expand to: 🗑️ Delete · ↩️ Reply · ↪️ Forward

---

## Project structure

```
corti-bot/
├── bot/
│   ├── main.py          # Telegram handlers + dispatcher
│   ├── router.py        # LLM intent classifier
│   ├── voice.py         # Whisper STT
│   ├── llm.py           # Groq client
│   └── services/
│       ├── gmail.py
│       ├── calendar.py
│       ├── notion.py
│       └── todoist.py
├── credentials/         # google_token.pickle (gitignored)
├── data/                # PicklePersistence storage (gitignored)
├── setup_google_auth.py # One-time local OAuth setup
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/masha-el/corti-bot.git
cd corti-bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Collect API keys

| Key | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather on Telegram |
| `ALLOWED_USER_ID` | @userinfobot on Telegram |
| `GROQ_API_KEY` | console.groq.com |
| `NOTION_TOKEN` | notion.so/my-integrations → New integration |
| `NOTION_PARENT_PAGE_ID` | Target Notion page URL → copy the ID (optional) |
| `TODOIST_API_KEY` | todoist.com → Settings → Integrations → Developer |

```bash
cp .env.example .env
# Fill in all values
```

### 3. Google OAuth (one-time, run locally)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → enable **Gmail API** and **Google Calendar API**
3. Create OAuth 2.0 credentials (Desktop app type) → download JSON
4. Save as `credentials/google_credentials.json`
5. Run:

```bash
python setup_google_auth.py
```

Browser opens → authorize → `credentials/google_token.pickle` is created.

### 4. Notion integration

1. Go to [notion.so/my-integrations](https://notion.so/my-integrations) → New integration
2. Enable: Read, Update, Insert content
3. Copy the secret → add to `.env` as `NOTION_TOKEN`
4. Share your Notion pages with the integration (Share → Invite → Corti)

### 5. Run locally

```bash
python -m bot.main
```

### 6. Run with Docker

```bash
docker compose up -d
docker compose logs -f
```

---

## Deployment (GCP + GitHub Actions)

The CI/CD pipeline builds the Docker image, pushes it to GHCR, and deploys to a GCP VM via IAP tunnel on every push to `main`.

### GitHub Actions secrets required

| Secret | Description |
|---|---|
| `GCP_SA_KEY` | GCP service account JSON |
| `GCP_DEPLOY_SSH_KEY` | SSH private key for VM access |
| `GCP_VM_USER` | VM SSH username |
| `GCP_INSTANCE_NAME` | GCP VM instance name |
| `GCP_ZONE` | GCP zone (e.g. `us-central1-f`) |
| `GCP_VM_BOT_DIR` | Bot directory on VM (e.g. `/opt/corti-bot`) |
| `CR_PAT` | GitHub personal access token (for GHCR pull) |

### VM setup (one-time)

```bash
# Create bot directory
sudo mkdir -p /opt/corti-bot
sudo chown your_user:docker /opt/corti-bot

# Add docker-compose.yml and .env
# Upload google_token.pickle
gcloud compute scp credentials/google_token.pickle \
  user@instance:/opt/corti-bot/credentials/ \
  --zone=your_zone --tunnel-through-iap
```

---

## Notes

- Only the user with `ALLOWED_USER_ID` can interact with the bot
- Voice messages up to Telegram's file size limit are supported
- Google OAuth token auto-refreshes — no re-auth needed after initial setup
- State persists across restarts via `PicklePersistence`
- Any in-progress conversation flow is cleared on restart to prevent stale state

---

## License

MIT