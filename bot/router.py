import json
import re
import logging
from datetime import date, timedelta, datetime
from bot.llm import chat

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are an intent classifier for a personal assistant Telegram bot.
Today is {weekday}, {today}.

Upcoming dates — use ONLY these values when resolving day names:
{date_map}

Classify the user message into exactly one intent and extract parameters.
Respond ONLY with a valid JSON object. No markdown, no explanation, no extra text.

Available intents:

gmail_read:         {{"intent": "gmail_read",         "params": {{"query": "", "max_results": 5}}}}
gmail_send:         {{"intent": "gmail_send",         "params": {{"to": "email@example.com", "subject": "...", "body": "..."}}}}
calendar_read:      {{"intent": "calendar_read",      "params": {{"date": "today|tomorrow|this week|next week|DD-MM-YYYY", "period": "day|week"}}}}
calendar_write:     {{"intent": "calendar_write",     "params": {{"title": "...", "date": "DD-MM-YYYY", "time": "HH:MM", "duration_minutes": 60}}}}
calendar_delete:    {{"intent": "calendar_delete",    "params": {{"title": "...", "date": "DD-MM-YYYY"}}}}
calendar_edit:      {{"intent": "calendar_edit",      "params": {{"title": "...", "date": "DD-MM-YYYY", "new_title": "...", "new_date": "DD-MM-YYYY", "new_time": "HH:MM", "new_duration_minutes": 60}}}}
notion_search:      {{"intent": "notion_search",      "params": {{"query": "..."}}}}
notion_write:       {{"intent": "notion_write",       "params": {{"title": "...", "content": "..."}}}}
notion_read:        {{"intent": "notion_read",        "params": {{"query": "..."}}}}
todoist_read:       {{"intent": "todoist_read",       "params": {{"filter": "today|upcoming|overdue|all"}}}}
todoist_write:      {{"intent": "todoist_write",      "params": {{"content": "...", "due_date": "DD-MM-YYYY or null", "priority": 3}}}}
todoist_complete:   {{"intent": "todoist_complete",   "params": {{"task_name": "..."}}}}
todoist_delete:     {{"intent": "todoist_delete",     "params": {{"task_name": "..."}}}}
freeform:           {{"intent": "freeform",           "params": {{"message": "original user message"}}}}

Rules:
- A week runs Sunday to Saturday.
- "this week" means from this Sunday to this Saturday (inclusive).
- For "this week" queries, set date to "this week" and period to "week".
- "next week" means from next Sunday to next Saturday (inclusive).
- For "next week" queries, set date to "next week" and period to "week".
- Never calculate week dates yourself — always use the literal strings "this week" or "next week".
- calendar_write: default time to "09:00" if not specified. Default duration to 60.
- todoist_write priority: 1=urgent 2=high 3=normal 4=low. Default to 3.
- gmail_read: if no specific query, use empty string.
- gmail_read: extract search terms into query using Gmail syntax where possible.
  Examples: "emails from Dan" → "from:dan", "unread emails" → "is:unread",
  "emails about interview" → "subject:interview"
- gmail_send: if subject or body is missing, leave as empty string — the dispatcher will ask.
- freeform: use only when no other intent clearly matches.
- calendar_delete: extract the event title and date from the user message.
- calendar_edit: only include fields the user explicitly wants to change. Omit new_title, new_date, new_time, new_duration_minutes if not mentioned.
- notion_search: use when user wants to find or look up a note.
- notion_read: use when user wants to read or see the content of a specific note.
- notion_write: use when user wants to save or create a note.
- todoist_write priority: 1=urgent 2=high 3=normal 4=low. Default to 3.
- todoist_complete: extract the task name the user wants to mark as done.
- todoist_delete: extract the task name the user wants to delete.
- due_date: resolve natural language to DD-MM-YYYY. Null if not specified.
"""

def _build_date_map() -> str:
    today = date.today()
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    lines = []
    for i in range(7):
        d = today + timedelta(days=i)
        lines.append(f"  {d.strftime('%A')}: {d.strftime('%d-%m-%Y')}")
    return "/n".join(lines)

def route(user_message: str) -> dict:
    today = date.today()
    system = _SYSTEM_PROMPT.format(
        today=today.strftime("%d-%m-%Y"),
        weekday=today.strftime("%A"),
        date_map=_build_date_map()
    )

    response = chat(
        messages=[{"role": "user", "content": user_message}],
        system=system,
    )

    # Strip markdown fences if LLM adds them despite instructions
    clean = re.sub(r"```(?:json)?|```", "", response).strip()

    try :
        return json.loads(clean)
    except json.JSONDecodeError:
        logger.warning(f"Router returned invalid JSON: {response!r}. Falling back to freeform.")
        return {"intent": "freeform", "params": {"message": user_message}}
    
def resolve_date(text: str) -> str:
    """
    Takes any natural language date expression and returns DD-MM-YYYY.
    Falls back to the original text if resolution fails.
    """
    today = date.today()
    date_map = _build_date_map()

    system = f"""You are a date resolver. Today is {today.strftime('%A')}, {today.strftime('%d-%m-%Y')}.

Upcoming dates:
{date_map}

The user will give you a date expression in natural language.
Return ONLY the date in DD-MM-YYYY format. No explanation, no extra text.

Examples:
- "friday" → "11-04-2026"
- "next monday" → "13-04-2026"
- "tomorrow" → "09-04-2026"
- "10-04-2026" → "10-04-2026"
"""

    response = chat(
        messages=[{"role": "user", "content": text}],
        system=system,
    )
    resolved = response.strip()

    # Validate it looks like a date
    try:
        datetime.strptime(resolved, "%d-%m-%Y")
        return resolved
    except ValueError:
        logger.warning(f"resolve_date got non-date response: {resolved!r}. Using original.")
        return text