import os
import logging
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.ext import PicklePersistence, CommandHandler, CallbackQueryHandler

from bot.router import route, resolve_date
from bot.voice import transcribe
from bot.services import calendar, gmail
from bot.services import notion, todoist
from bot.llm import chat


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

REQUIRED_FIELDS = {
    "calendar_delete": ["title", "date"],
    "calendar_edit":   ["title", "date"],
    "calendar_write":  ["title", "date"],
    "gmail_send":      ["to", "subject", "body"],
    "notion_write":    ["title", "content"],
    "todoist_write":   ["content"],
    "todoist_complete": ["task_name"],
    "todoist_delete":   ["task_name"],
}

CLARIFICATION_PROMPTS = {
    "calendar_delete": {
        "title": "What's the name of the event you want to delete?",
        "date":  "What date is it on?",
    },
    "calendar_edit": {
        "title": "What's the name of the event you want to edit?",
        "date":  "What date is it on?",
    },
    "calendar_write": {
        "title": "What should I call the event?",
        "date":  "What date?",
    },
    "gmail_send": {
        "to":      "Who should I send it to? (email address)",
        "subject": "What's the subject?",
        "body":    "What should the email say?",
    },
    "notion_write": {
        "title":   "What should the note be called?",
        "content": "What should the note say?",
    },
    "todoist_write": {
        "content": "What's the task?",
    },
    "todoist_complete": {
        "task_name": "Which task do you want to mark as done?",
    },
    "todoist_delete": {
        "task_name": "Which task do you want to delete?",
    },
}

AMBIGUOUS_ACTIONS = {
    # Calendar
    "cal del":    "calendar_delete",
    "cal remove": "calendar_delete",
    "cal edit":   "calendar_edit",
    "cal update": "calendar_edit",
    "cal add":    "calendar_write",
    "cal create": "calendar_write",

    # Gmail
    "gmail send":  "gmail_send",
    "gmail reply": "gmail_send",

    # Notion
    "noti write":  "notion_write",
    "noti save":   "notion_write",
    "noti search": "notion_search",

    # Todoist
    "todo add":    "todoist_write",
    "todo done":   "todoist_complete",
    "todo del":    "todoist_delete",
}

EDIT_FIELDS = {
    "title":    "What should the new title be?",
    "date":     "What's the new date? (DD-MM-YYYY or natural language)",
    "time":     "What's the new time? (HH:MM)",
    "duration": "What's the new duration in minutes?",
}

PRIORITY_EMOJI = {4: "🔴", 3: "🟠", 2: "🔵", 1: "⚪"}

def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")]
    ])

def _confirm_delete_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑️ Yes, delete", callback_data="confirm_delete"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit"),
        ]
    ])

def _detect_ambiguous_action(text: str) -> tuple[str | None, str]:
    """
    Returns (intent, remaining_text).
    remaining_text is the message with the prefix stripped.
    If no prefix matched, returns (None, original_text).
    """
    lowered = text.strip().lower()
    for prefix, intent in AMBIGUOUS_ACTIONS.items():
        if lowered.startswith(prefix):
            remaining = text[len(prefix):].strip()
            return intent, remaining
    return None, text

def _get_missing_field(intent: str, params: dict) -> str | None:
    """Returns the first missing required field, or None if all present."""
    required = REQUIRED_FIELDS.get(intent, [])
    for field in required:
        if not params.get(field):
            return field
    return None

def _get_clarification_prompt(intent: str, missing_field: str) -> str:
    default = f"Can you give me more information? I'm missing: {missing_field}"
    return CLARIFICATION_PROMPTS.get(intent, {}).get(missing_field, default)

def _fmt_events(events: list[dict]) -> str:
    if not events:
        return "📭 No events found."
    lines = []
    for e in events:
        # Parse the ISO datetime string
        start_raw = e["start"]
        try:
            # Removing timezone offset before parsing
            dt = datetime.fromisoformat(start_raw)
            formatted_start = dt.strftime("%A on %d-%m-%Y at %H:%M")
        except ValueError:
            # All-day events have no time — just a date string "YYYY-MM-DD"
            dt = datetime.strptime(start_raw, "%Y-%m-%d") #match Google's format
            formatted_start = dt.strftime("%A on %d-%m-%Y")

        loc = f"\n📍 {e['location']}" if e["location"] else ""
        lines.append(f"🗓️ *{e['title']}*\n{formatted_start}{loc}")
    return "\n\n".join(lines)

def _fmt_event_dt(start_raw: str) -> str:
    try:
        dt = datetime.fromisoformat(start_raw)
        return dt.strftime("%A %d-%m-%Y at %H:%M")
    except ValueError:
        try:
            dt = datetime.strptime(start_raw, "%Y-%m-%d")
            return dt.strftime("%A %d-%m-%Y")
        except ValueError:
            return start_raw
        
def _fmt_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "✅ No tasks found."
    lines = []
    for t in tasks:
        emoji = PRIORITY_EMOJI.get(t["priority"], "⚪")
        lines.append(f"{emoji} {t['content']} — {t['due']}")
    return "\n".join(lines)
        
async def _send_emails(update: Update, emails: list[dict], context: ContextTypes.DEFAULT_TYPE) -> None:
    if not emails:
        await update.message.reply_text("📭 No emails found.")
        return

    for e in emails:
        text = (
            f"📧 *{e['subject']}*\n"
            f"From: {e['from']}\n"
            f"_{e['snippet']}_"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⬇️ Show more", callback_data=f"email_more_{e['id']}"),
                InlineKeyboardButton("Actions »", callback_data=f"email_actions_{e['id']}"),
            ]
        ])
        # Store snippet for "show less" restoration
        context.user_data[f"email_snippet_{e['id']}"] = {
            "subject": e["subject"],
            "from":    e["from"],
            "snippet": e["snippet"],
        }
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    
async def _send_notion_results(update: Update, pages: list[dict], ask_pick: bool = False) -> None:
    lines = []
    for i, p in enumerate(pages):
        lines.append(f"{i+1}. ↳ [{p['title']}]({p['url']})")
    
    text = "\n".join(lines)
    if ask_pick:
        text += "\n\nWhich one do you want to read? Reply with the number."
    
    await update.message.reply_text(text, parse_mode="Markdown")

def _confirm_todoist_keyboard(task_id: str, action: str) -> InlineKeyboardMarkup:
    confirm_text = "✅ Yes, complete" if action == "complete" else "🗑️ Yes, delete"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(confirm_text, callback_data=f"todoist_{action}_{task_id}"),
            InlineKeyboardButton("❌ Cancel",  callback_data="cancel_edit"),
        ]
    ])

async def handle_edit_conversation(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str
) -> bool:
    edit_state = context.user_data.get("edit_state")

    # ── Step 1: show this week's events ─────
    if edit_state == "awaiting_edit_pick":
        candidates = context.user_data.get("edit_candidates", [])

        if text.strip().isdigit():
            choice = int(text.strip()) - 1
            if choice < 0 or choice >= len(candidates):
                await update.message.reply_text(
                    f"Please reply with a number between 1 and {len(candidates)}.",
                    reply_markup=_cancel_keyboard()
                )
                return True
        
            event = candidates[choice]
            context.user_data["edit_event"] = event
            context.user_data["edit_state"] = "awaiting_field"

            start_raw = event["start"].get("dateTime", event["start"].get("date", ""))
            dt = datetime.fromisoformat(start_raw)
            formatted = dt.strftime("%A %d-%m-%Y at %H:%M")

            await update.message.reply_text(
                f"Got it: *{event.get('summary')}* — {formatted}\n\n"
                f"What do you want to change?\n• title\n• date\n• time\n• duration",
                parse_mode="Markdown",
                reply_markup=_cancel_keyboard()
            )
            return True
        
        # User typed a name instead of a number
        context.user_data["edit_title"] = text
        context.user_data["edit_state"] = "awaiting_date"
        await update.message.reply_text(
            "What date is it on?",
            reply_markup=_cancel_keyboard()
        )
        return True
    
    # ── Step 2: received date — search for event ──────────
    if edit_state == "awaiting_date":
        # Use router to resolve natural language date
        date_str = resolve_date(text)

        title = context.user_data.get("edit_title")
        event = calendar.find_event(title, date_str)
        
        if event is None:
            await update.message.reply_text(
                f"❌ No event found matching '{title}' on {date_str}. Try again.",
                reply_markup=_cancel_keyboard()
            )
            context.user_data.clear()
            return True

        if isinstance(event, list):
            # Multiple matches — store them and ask for clarification
            context.user_data["edit_candidates"] = event
            context.user_data["edit_state"] = "awaiting_clarification"

            lines = "\n".join(
                f"{i+1}. {e.get('summary')} at "
                f"{datetime.fromisoformat(e['start'].get('dateTime', '')).strftime('%H:%M') if e['start'].get('dateTime') else 'all day'}"
                for i, e in enumerate(event)
            )
            await update.message.reply_text(
                f"I found several events, which one did you mean?\n\n{lines}\n\nReply with the number.",
                parse_mode="Markdown",
                reply_markup=_cancel_keyboard()
            )
            return True
        
        # Single match
        context.user_data["edit_event"] = event
        context.user_data["edit_state"] = "awaiting_field"

        # Show the found event and ask what to change
        start_raw = event["start"].get("dateTime", event["start"].get("date", ""))
        try:
            dt = datetime.fromisoformat(start_raw)
            formatted = dt.strftime("%A %d-%m-%Y at %H:%M")
        except ValueError:
            formatted = start_raw

        await update.message.reply_text(
            f"Found: *{event.get('summary')}* — {formatted}\n\n"
            f"What do you want to change?\n"
            f"• title\n• date\n• time\n• duration",
            parse_mode="Markdown",
            reply_markup=_cancel_keyboard()
        )
        return True
    
    # ── Step 3: handle the user's number reply ─────────
    if edit_state == "awaiting_clarification":
        candidates = context.user_data.get("edit_candidates", [])
        try:
            choice = int(text.strip()) - 1
            if choice < 0 or choice >= len(candidates):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"Please reply with a number between 1 and {len(candidates)}.",
                reply_markup=_cancel_keyboard()
            )
            return True

        event = candidates[choice]
        context.user_data["edit_event"] = event
        context.user_data["edit_state"] = "awaiting_field"

        start_raw = event["start"].get("dateTime", event["start"].get("date", ""))
        try:
            dt = datetime.fromisoformat(start_raw)
            formatted = dt.strftime("%A %d-%m-%Y at %H:%M")
        except ValueError:
            formatted = start_raw

        await update.message.reply_text(
            f"Got it: *{event.get('summary')}* — {formatted}\n\n"
            f"What do you want to change?\n• title\n• date\n• time\n• duration",
            parse_mode="Markdown",
            reply_markup=_cancel_keyboard()
        )
        return True
    
    # ── Step 4: received field to change ─────────
    if edit_state == "awaiting_field":
        field = text.strip().lower()
        if field not in EDIT_FIELDS:
            await update.message.reply_text(
                f"Please choose one of: title, date, time, duration",
                reply_markup=_cancel_keyboard()
            )
            return True

        context.user_data["edit_field"] = field
        context.user_data["edit_state"] = "awaiting_value"
        await update.message.reply_text(EDIT_FIELDS[field], reply_markup=_cancel_keyboard())
        return True

    # ── Step 5: received new value — execute update ───
    if edit_state == "awaiting_value":
        event = context.user_data.get("edit_event")
        field = context.user_data.get("edit_field")

        # Resolve natural language for date/time fields
        if field == "date":
            new_value = resolve_date(text)
        elif field == "time":
            new_value = text.strip()
        else:
            new_value = text.strip()

        try:
            updated_title = calendar.update_event_field(event, field, new_value)
            await update.message.reply_text(
                f"✅ *{updated_title}* updated successfully.",
                parse_mode="Markdown",
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Update failed: {e}")

        context.user_data.clear()
        return True

    return False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id != ALLOWED_USER_ID:
        logger.warning(f"Unauthorized access from user_id={user_id}")
        return
    
    # Resolve input: voice → transcript, text → direct
    if update.message.voice:
        await update.message.reply_text("🎙️ Transcribing...")
        try:
            text = await transcribe(update.message.voice, context.bot)
        except Exception as e:
            await update.message.reply_text(f"❌ Transcription failed: {e}")
            return
        await update.message.reply_text(f"📝 Heard: {text}")
    else:
        text = update.message.text

    # Delegate to edit conversation if active
    if context.user_data.get("edit_state"):
        handled = await handle_edit_conversation(update, context, text)
        if handled:
            return
    
    if context.user_data.get("delete_state"):
        handled = await handle_delete_conversation(update, context, text)
        if handled:
            return
        
    if context.user_data.get("todoist_state"):
        handled = await handle_todoist_action_conversation(update, context, text)
        if handled:
            return
    
    # Handle notion pick response
    if context.user_data.get("notion_candidates"):
        if text.strip().isdigit():
            candidates = context.user_data.get("notion_candidates", [])
            choice = int(text.strip()) - 1

            if choice < 0 or choice >= len(candidates):
                await update.message.reply_text(
                    f"Please reply with a number between 1 and {len(candidates)}."
                )
                return
            
            page = candidates[choice]
            action = context.user_data.get("notion_action", "read")
            context.user_data.pop("notion_candidates", None)
            context.user_data.pop("notion_action", None)

            if action == "read":
                content = notion.get_page_content(page["id"])
                if len(content) > 4000:
                    content = content[:4000] + "\n\n[truncated]"
                await update.message.reply_text(
                    f"📄 *{page['title']}*\n\n{content}",
                    parse_mode="Markdown"
                )
                return
            
    # Notion parent page pick
    if context.user_data.get("notion_pending") and context.user_data.get("notion_parent_candidates"):
        candidates = context.user_data.get("notion_parent_candidates", [])

        if not text.strip().isdigit():
            await update.message.reply_text(
                f"Please reply with a number between 1 and {len(candidates)}.",
                reply_markup=_cancel_keyboard()
            )
            return

        choice = int(text.strip()) - 1
        if choice < 0 or choice >= len(candidates):
            await update.message.reply_text(
                f"Please reply with a number between 1 and {len(candidates)}.",
                reply_markup=_cancel_keyboard()
            )
            return
        
        parent = candidates[choice]
        pending = context.user_data.get("notion_pending")

        context.user_data.pop("notion_pending", None)
        context.user_data.pop("notion_parent_candidates", None)

        try:
            url = notion.create_page(
                title=pending["title"],
                content=pending["content"],
                parent_id=parent["id"],
            )
            await update.message.reply_text(
                f"✅ Note *{pending['title']}* saved under *{parent['title']}*.\n{url}",
                parse_mode="Markdown"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to create note: {e}")
        return
    
    # Todoist: multiple task candidates
    if context.user_data.get("todoist_candidates"):
        candidates = context.user_data.get("todoist_candidates", [])

        if not text.strip().isdigit():
            await update.message.reply_text(
                f"Please reply with a number between 1 and {len(candidates)}.",
                reply_markup=_cancel_keyboard()
            )
            return
        
        choice = int(text.strip()) - 1
        if choice < 0 or choice >= len(candidates):
            await update.message.reply_text(
                f"Please reply with a number between 1 and {len(candidates)}.",
                reply_markup=_cancel_keyboard()
            )
            return
        
        task = candidates[choice]
        action = context.user_data.get("todoist_action")
        context.user_data.pop("todoist_candidates", None)
        context.user_data.pop("todoist_action", None)

        confirm_text = "Mark as done?" if action == "complete" else "🗑️ Delete this task?"
        await update.message.reply_text(
            f"{confirm_text} *{task['content']}*",
            parse_mode="Markdown",
            reply_markup=_confirm_todoist_keyboard(task["id"], action)
        )
        return

    # Check if bot is waiting for basic user info
    if context.user_data.get("awaiting") == "name":
        context.user_data["name"] = text
        context.user_data.pop("awaiting")
        await update.message.reply_text(f"Got it, {text}! I'll remember that. 🧠")
        return
    
    # Check for pending intent from previous message
    pending = context.user_data.get("pending_intent")

    if pending:
        # User answers -> extract missing field value
        follow_up_result = route(text)
        follow_up_params = follow_up_result.get("params", {})

        # Merge new params to existing ones
        stored_params = context.user_data.get("pending_params", {})
        stored_params.update({k: v for k, v in follow_up_params.items() if v})

        # Treating the raw text as the missing field directly
        missing_field = context.user_data.get("pending_missing_field")
        if missing_field and not stored_params.get(missing_field):
            stored_params[missing_field] = text

        intent = pending
        params = stored_params
    else:
        # Check for ambiguous prefix commands
        ambiguous_intent, remaining_text = _detect_ambiguous_action(text)
        if ambiguous_intent:
            if ambiguous_intent in ("calendar_edit", "calendar_delete") and not remaining_text:
                events = calendar.get_raw_events("this week", "week")

                if not events:
                    state_key = "delete_state" if ambiguous_intent == "calendar_delete" else "edit_state"
                    first_state = "awaiting_delete_title" if ambiguous_intent == "calendar_delete" else "awaiting_title"
                    context.user_data[state_key] = first_state
                    await update.message.reply_text(
                        "No events found this week. Type the name of the event.",
                        reply_markup=_cancel_keyboard()
                    )
                    return
                
                context.user_data["delete_candidates" if ambiguous_intent == "calendar_delete" else "edit_candidates"] = events

                if ambiguous_intent == "calendar_delete":
                    context.user_data["delete_state"] = "awaiting_delete_pick"
                else:
                    context.user_data["edit_state"] = "awaiting_edit_pick"
                
                lines = "\n".join(
                    f"{i+1}. {e.get('summary')} on "
                    f"{datetime.fromisoformat(e['start'].get('dateTime', e['start'].get('date', ''))).strftime('%A %d-%m-%Y at %H:%M')}"
                    for i, e in enumerate(events)
                )

                action = "delete" if ambiguous_intent == "calendar_delete" else "edit"
                await update.message.reply_text(
                    f"Here are your events this week. If the event is not listed, just type its name:\n\n{lines}\n\nType the number of the event you want to {action}.",
                    reply_markup=_cancel_keyboard()
                )
                return
              
            if ambiguous_intent in ("todoist_complete", "todoist_delete") and not remaining_text:
                all_tasks = todoist.read_tasks("all")

                if not all_tasks:
                    await update.message.reply_text("✅ No active tasks found.")
                    return

                action = "complete" if ambiguous_intent == "todoist_complete" else "delete"
                context.user_data["todoist_action"] = action
                context.user_data["todoist_candidates"] = all_tasks
                context.user_data["todoist_state"] = "awaiting_todoist_pick"

                lines = "\n".join(
                    f"{i+1}. {t['content']} — {t['due']}"
                    for i, t in enumerate(all_tasks)
                )
                action_label = "mark as done" if action == "complete" else "delete"
                await update.message.reply_text(
                    f"Which task do you want to {action_label}?\n\n{lines}\n\nReply with a number.",
                    reply_markup=_cancel_keyboard()
                )
                return
                   
            if remaining_text:
                result = route(remaining_text)
                intent = ambiguous_intent
                params = result.get("params", {})
            else:
                intent = ambiguous_intent
                params = {}
        else:
            # Route intent — same for both text and voice
            result = route(text)
            intent = result.get("intent")
            params = result.get("params", {})

    # Check if required fields are still missing
    missing_field = _get_missing_field(intent, params)

    if missing_field:
        # Store state and ask follow-up
        context.user_data["pending_intent"] = intent
        context.user_data["pending_params"] = params
        context.user_data["pending_missing_field"] = missing_field
        prompt = _get_clarification_prompt(intent, missing_field)
        await update.message.reply_text(prompt, reply_markup=_cancel_keyboard())
        return

    # All fields present — clear pending state and dispatch
    context.user_data.clear()
    logger.info(f"Intent: {intent} | Params: {params}")

    try:
        if intent == "calendar_read":
            events = calendar.read_events(
                params.get("date", "today"),
                params.get("period", "day")
                )
            await update.message.reply_text(_fmt_events(events), parse_mode="Markdown")
        
        elif intent == "calendar_write":
            resolved_date = resolve_date(params["date"])
            calendar.create_event(
                title=params["title"],
                date=resolved_date,
                time=params.get("time", "09:00"),
                duration_minutes=params.get("duration_minutes", 60),
            )
            await update.message.reply_text(
                f"✅ *{params['title']}* added\n{resolved_date} at {params.get('time', '09:00')}",
                parse_mode="Markdown",
            )
        elif intent == "calendar_delete":
            msg = calendar.delete_event(params["title"], params["date"])
            await update.message.reply_text(msg, parse_mode="Markdown")
        
        elif intent == "calendar_edit":
            msg = calendar.edit_event(
                title=params["title"],
                date=params["date"],
                new_title=params.get("new_title"),
                new_date=params.get("new_date"),
                new_time=params.get("new_time"),
                new_duration_minutes=params.get("new_duration_minutes"),
            )
            await update.message.reply_text(msg, parse_mode="Markdown")

        elif intent == "freeform":
                name = context.user_data.get("name", "")
                system = (
                    f"You are Corti, a personal assistant Telegram bot{f' for {name}' if name else ''}. "
                    "You help the user manage their Gmail, Google Calendar, Notion and Todoist. "
                    "Available shortcuts: cal add, cal edit, cal delete, gmail send, "
                    "noti write, noti search, todo add, todo done, todo del. "
                    "You also understand natural language for all actions. "
                    "Answer concisely and directly."
                )
                response = chat(
                    messages=[{"role": "user", "content": params.get("message", text)}],
                    system=system,
                )
                await update.message.reply_text(response)
        
        elif intent == "gmail_read":
            emails = gmail.read_emails(
                query=params.get("query", ""),
                max_results=min(params.get("max_results", 5), 5),
                unread_only=params.get("unread_only", True),
            )
            await _send_emails(update, emails, context)
        
        elif intent == "gmail_send":
            gmail.send_email(params["to"], params["subject"], params["body"])
            await update.message.reply_text(
                f"✅ Email sent to *{params['to']}*",
                parse_mode="Markdown"
            )

        elif intent == "notion_search":
            pages = notion.search_pages(params.get("query", ""))
            if not pages:
                await update.message.reply_text("📭 No Notion pages found.")
            else:
                await _send_notion_results(update, pages)
        
        elif intent == "notion_read":
            pages = notion.search_pages(params.get("query", ""))
            if not pages:
                await update.message.reply_text("📭 No pages found matching that query.")
            elif len(pages) == 1:
                content = notion.get_page_content(pages[0]["id"])
                await update.message.reply_text(
                    f"↳*{pages[0]['title']}*\n\n{content}",
                    parse_mode="Markdown"
                )
            else:
                # Multiple matches — show list and let user pick
                context.user_data["notion_candidates"] = pages
                context.user_data["notion_action"] = "read"
                await _send_notion_results(update, pages, ask_pick=True)
        
        elif intent == "notion_write":
            pages = notion.get_top_level_pages()

            if not pages:
                url = notion.create_page(
                    title=params["title"],
                    content=params.get("content", ""),
                )
                await update.message.reply_text(
                    f"✅ Note *{params['title']}* created.\n{url}",
                    parse_mode="Markdown"
                )
                return
            
            context.user_data["notion_pending"] = {
                "title":   params["title"],
                "content": params.get("content", ""),
            }
            context.user_data["notion_parent_candidates"] = pages

            lines = "\n".join(
                f"{i+1}. {p['title']}"
                for i, p in enumerate(pages)
            )
            await update.message.reply_text(
                f"Where do you want to save *{params['title']}*?\n\n{lines}\n\nReply with a number.",
                parse_mode="Markdown",
                reply_markup=_cancel_keyboard()
            )

        elif intent == "todoist_read":
            tasks = todoist.read_tasks(params.get("filter", "today"))
            await update.message.reply_text(
                _fmt_tasks(tasks)
            )

        elif intent == "todoist_write":
            due = resolve_date(params["due_date"]) if params.get("due_date") else None
            todoist.create_task(
                content=params["content"],
                due_date=due,
                priority=params.get("priority", 3),
            )
            await update.message.reply_text(
                f"✅ Task added: *{params['content']}*",
                parse_mode="Markdown"
            )

        elif intent == "todoist_complete":
            matches = todoist.find_tasks_by_name(params.get("task_name", ""))
            if not matches:
                await update.message.reply_text("❌ No matching task found.")
            elif len(matches) == 1:
                context.user_data["todoist_action"] = "complete"
                context.user_data["todoist_task"] = matches[0]
                await update.message.reply_text(
                    f"Mark *{matches[0]['content']}* as done?",
                    parse_mode="Markdown",
                    reply_markup=_confirm_todoist_keyboard(matches[0]["id"], "complete")
                )
            else:
                context.user_data["todoist_action"] = "complete"
                context.user_data["todoist_candidates"] = matches
                lines = "\n".join(
                    f"{i+1}. {t['content']} — _{t['due']}_"
                    for i, t in enumerate(matches)
                )
                await update.message.reply_text(
                    f"Found multiple tasks — which one?\n\n{lines}\n\nReply with a number.",
                    parse_mode="Markdown",
                    reply_markup=_cancel_keyboard()
                )
        
        elif intent == "todoist_delete":
            matches = todoist.find_tasks_by_name(params.get("task_name", ""))
            if not matches:
                await update.message.reply_text("❌ No matching task found.")
            elif len(matches) == 1:
                context.user_data["todoist_action"] = "delete"
                context.user_data["todoist_task"] = matches[0]
                await update.message.reply_text(
                    f"🗑️ Delete *{matches[0]['content']}*?",
                    parse_mode="Markdown",
                    reply_markup=_confirm_todoist_keyboard(matches[0]["id"], "delete")
                )
            else:
                context.user_data["todoist_action"] = "delete"
                context.user_data["todoist_candidates"] = matches
                lines = "\n".join(
                    f"{i+1}. {t['content']} — _{t['due']}_"
                    for i, t in enumerate(matches)
                )
                await update.message.reply_text(
                    f"Found multiple tasks — which one?\n\n{lines}\n\nReply with a number.",
                    parse_mode="Markdown",
                    reply_markup=_cancel_keyboard()
                )

        else:
            # Temporary: show raw intent for unimplemented services
            await update.message.reply_text(str({"intent": intent, "params": params}))
    
    except KeyError as e:
        await update.message.reply_text(f"❌ Missing required field: {e}")
    except Exception as e:
        logger.error(f"Service error [{intent}]: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {e}")

async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # removes the loading spinner on the button
    data = query.data

    # ── Cancel ────────────────────────
    if data == "cancel_edit":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelled.")

    # ── Delete confirm ───────────────────
    elif data == "confirm_delete":
        event = context.user_data.get("delete_event")
        if event:
            try:
                calendar.delete_event_by_id(event["id"])
                await query.edit_message_text(f"🗑️ *{event.get('summary')}* deleted.", parse_mode="Markdown")
            except Exception as e:
                await query.edit_message_text(f"❌ Failed to delete: {e}")
        context.user_data.clear()

    # ── Email: show more ─────────────────
    elif data.startswith("email_more_"):
        message_id = data.replace("email_more_", "")
        try:
            body = gmail.get_email_body(message_id)
            # Telegram has a 4096 char limit per message
            if len(body) > 4000:
                body = body[:4000] + "\n\n_[truncated]_"
            # Keep the actions keyboard so user can still act on the email
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬆️ Show less", callback_data=f"email_less_{message_id}"),
                ],
                [
                    InlineKeyboardButton("🗑️ Delete", callback_data=f"email_delete_{message_id}"),
                    InlineKeyboardButton("↩️ Reply",  callback_data=f"email_reply_{message_id}"),
                    InlineKeyboardButton("↪️ FWD",    callback_data=f"email_fwd_{message_id}"),
                ]
            ])
            await query.edit_message_text(body, reply_markup=keyboard)
        except Exception as e:
            await query.edit_message_text(f"❌ Could not load email: {e}")
    
    # Email: show less
    elif data.startswith("email_less_"):
        message_id = data.replace("email_less_", "")
        stored = context.user_data.get(f"email_snippet_{message_id}")

        if not stored:
            await query.answer("Preview no longer available.")
            return
        
        text = (
            f"📧 *{stored['subject']}*\n"
            f"From: {stored['from']}\n"
            f"_{stored['snippet']}_"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⬇️ Show more", callback_data=f"email_more_{message_id}"),
                InlineKeyboardButton("Actions »",    callback_data=f"email_actions_{message_id}"),
            ]
        ])
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )

    # ── Email: show actions ───────────
    elif data.startswith("email_actions_"):
        message_id = data.replace("email_actions_", "")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🗑️ Delete", callback_data=f"email_delete_{message_id}"),
                InlineKeyboardButton("↩️ Reply",  callback_data=f"email_reply_{message_id}"),
                InlineKeyboardButton("↪️ FWD",    callback_data=f"email_fwd_{message_id}"),
            ],
            [
                InlineKeyboardButton("✖️ Close",  callback_data=f"email_close"),
            ]
        ])
        # Only update the keyboard, keep the original message text
        await query.edit_message_reply_markup(reply_markup=keyboard)

    # ── Email: delete ──────────────────
    elif data.startswith("email_delete_"):
        message_id = data.replace("email_delete_", "")
        try:
            gmail.trash_email(message_id)
            await query.edit_message_text("🗑️ Email moved to trash.")
        except Exception as e:
            await query.message.reply_text(f"❌ Could not delete: {e}")

    # ── Email: reply ────────────────
    elif data.startswith("email_reply_"):
        message_id = data.replace("email_reply_", "")
        meta = gmail.get_email_sender(message_id)
        context.user_data["pending_intent"] = "gmail_send"
        context.user_data["pending_params"] = {
            "to": meta["reply_to"],
            "subject": f"Re: {meta['subject']}",
        }
        context.user_data["pending_missing_field"] = "body"
        await query.message.reply_text(
            f"Replying to: *{meta['reply_to']}*\n"
            f"Subject: *Re: {meta['subject']}*\n\n"
            f"What should the reply say?",
            parse_mode="Markdown",
            reply_markup=_cancel_keyboard()
        )
    
    # ── Email: forward ─────────────────
    elif data.startswith("email_fwd_"):
        message_id = data.replace("email_fwd_", "")
        meta = gmail.get_email_sender(message_id)
        context.user_data["email_fwd_id"] = message_id
        context.user_data["email_fwd_subject"] = f"Fwd: {meta['subject']}"
        context.user_data["pending_intent"] = "gmail_send"
        context.user_data["pending_params"] = {
            "subject": f"Fwd: {meta['subject']}",
        }
        context.user_data["pending_missing_field"] = "to"
        await query.message.reply_text(
            f"Forwarding: *{meta['subject']}*\n\nWho should I forward it to?",
            parse_mode="Markdown",
            reply_markup=_cancel_keyboard()
        )
    # ── Email: close actions menu ────────────
    elif data == "email_close":
        message_id = data.replace("email_close_", "")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⬇️ Show more", callback_data=f"email_more_{message_id}"),
                InlineKeyboardButton("Actions »",    callback_data=f"email_actions_{message_id}"),
            ]
        ])
        await query.edit_message_reply_markup(reply_markup=keyboard)
    
    # ── Todoist: task complete ───────
    elif data.startswith("todoist_complete_"):
        task_id = data.replace("todoist_complete_", "")
        try:
            todoist.complete_task(task_id)
            await query.edit_message_text("✅ Task marked as done.")
        except Exception as e:
            await query.edit_message_text(f"❌ Failed: {e}")
        context.user_data.clear()

    # ── Todoist: task delete ───────
    elif data.startswith("todoist_delete_"):
        task_id = data.replace("todoist_delete_", "")
        try:
            todoist.delete_task(task_id)
            await query.edit_message_text("🗑️ Task deleted.")
        except Exception as e:
            await query.edit_message_text(f"❌ Failed: {e}")
        context.user_data.clear()

async def handle_delete_conversation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str
) -> bool:

    delete_state = context.user_data.get("delete_state")

    # ── Step 1: show this week's events ──────────────────────────────────────────
    if delete_state == "awaiting_delete_pick":
        candidates = context.user_data.get("delete_candidates", [])

        # Check if user typed a number
        if text.strip().isdigit():
            choice = int(text.strip()) - 1
            if choice < 0 or choice >= len(candidates):
                await update.message.reply_text(
                    f"Please reply with a number between 1 and {len(candidates)}, or type an event name.",
                    reply_markup=_cancel_keyboard()
                )
                return True
            
            event = candidates[choice]
            context.user_data["delete_event"] = event
            context.user_data["delete_state"] = "awaiting_delete_confirm"

            start_raw = event["start"].get("dateTime", event["start"].get("date", ""))
            dt = datetime.fromisoformat(start_raw)
            formatted = dt.strftime("%A %d-%m-%Y at %H:%M")

            await update.message.reply_text(
                f"Are you sure you want to delete *{event.get('summary')}* on {formatted}?",
                parse_mode="Markdown",
                reply_markup=_confirm_delete_keyboard()
            )
            return True
        
        # User typed a name instead — move to date flow
        context.user_data["delete_title"] = text
        context.user_data["delete_state"] = "awaiting_delete_date"
        await update.message.reply_text(
            "What date is it on?",
            reply_markup=_cancel_keyboard()
        )
        return True

    # ── Step 2: received date — search for event ─────────────────────────────
    if delete_state == "awaiting_delete_date":
        date_str = resolve_date(text)
        title = context.user_data.get("delete_title")
        event = calendar.find_event(title, date_str)

        if event is None:
            await update.message.reply_text(
                f"❌ No event found matching '{title}' on {date_str}.",
            )
            context.user_data.clear()
            return True

        if isinstance(event, list):
            context.user_data["delete_candidates"] = event
            context.user_data["delete_state"] = "awaiting_delete_choice"

            lines = "\n".join(
                f"{i+1}. {e.get('summary')} on "
                f"{datetime.fromisoformat(e['start']['dateTime']).strftime('%A %d-%m-%Y at %H:%M')}"
                for i, e in enumerate(event)
            )
            await update.message.reply_text(
                f"I found multiple events — choose the number of the event you want to delete:\n\n{lines}",
                reply_markup=_cancel_keyboard()
            )
            return True

        # Single match — confirm before deleting
        context.user_data["delete_event"] = event
        context.user_data["delete_state"] = "awaiting_delete_confirm"

        start_raw = event["start"].get("dateTime", event["start"].get("date", ""))
        dt = datetime.fromisoformat(start_raw)
        formatted = dt.strftime("%A %d-%m-%Y at %H:%M")

        await update.message.reply_text(
            f"Are you sure you want to delete *{event.get('summary')}* on {formatted}?",
            parse_mode="Markdown",
            reply_markup=_confirm_delete_keyboard()
        )
        return True

    # ── Step 3a: multiple events — user picks a number ───────────────────────
    if delete_state == "awaiting_delete_choice":
        candidates = context.user_data.get("delete_candidates", [])
        try:
            choice = int(text.strip()) - 1
            if choice < 0 or choice >= len(candidates):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"Please reply with a number between 1 and {len(candidates)}.",
                reply_markup=_cancel_keyboard()
            )
            return True

        event = candidates[choice]
        context.user_data["delete_event"] = event
        context.user_data["delete_state"] = "awaiting_delete_confirm"

        start_raw = event["start"].get("dateTime", event["start"].get("date", ""))
        dt = datetime.fromisoformat(start_raw)
        formatted = dt.strftime("%A %d-%m-%Y at %H:%M")

        await update.message.reply_text(
            f"Are you sure you want to delete *{event.get('summary')}* on {formatted}?",
            parse_mode="Markdown",
            reply_markup=_confirm_delete_keyboard()
        )
        return True

    # ── Step 3b: confirmation via button — handled in callback handler ────────
    if delete_state == "awaiting_delete_confirm":
        await update.message.reply_text(
            "Please use the buttons above to confirm or cancel.",
            reply_markup=_cancel_keyboard()
        )
        return True

    return False

async def handle_todoist_action_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    todoist_state = context.user_data.get("todoist_state")

    # ── Step 1: user picks a number from the list ──────
    if todoist_state == "awaiting_todoist_pick":
        candidates = context.user_data.get("todoist_candidates", [])

        if not text.strip().isdigit():
            await update.message.reply_text(
                f"Please reply with a number between 1 and {len(candidates)}.",
                reply_markup=_cancel_keyboard()
            )
            return True
        
        choice = int(text.strip()) - 1
        if choice < 0 or choice >= len(candidates):
            await update.message.reply_text(
                f"Please reply with a number between 1 and {len(candidates)}.",
                reply_markup=_cancel_keyboard()
            )
            return True
        
        task = candidates[choice]
        action = context.user_data.get("todoist_action")
        context.user_data["todoist_task"] = task

        if action == "complete":
            await update.message.reply_text(
                f"Mark *{task['content']}* as done?",
                parse_mode="Markdown",
                reply_markup=_confirm_todoist_keyboard(task["id"], "complete")
            )
        else:
            await update.message.reply_text(
                f"🗑️ Delete *{task['content']}*?",
                parse_mode="Markdown",
                reply_markup=_confirm_todoist_keyboard(task["id"], "delete")
            )
        context.user_data.pop("todoist_state", None)
        context.user_data.pop("todoist_candidates", None)
        return True
    
    return False

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ALLOWED_USER_ID:
        return
    
    menu_text = (
        "📋 *Corti Commands Menu*\n\n"
        
        "📅 *Calendar*\n"
        "`cal add` — add a new event\n"
        "`cal edit` — edit an existing event\n"
        "`cal delete` — delete an event\n\n"

        "📧 *Gmail*\n"
        "`gmail send` — send an email\n\n"

        "📄 *Notion*\n"
        "`noti write` — create a new note\n"
        "`noti search` — search notes\n\n"

        "✅ *Todoist*\n"
        "`todo add` — add a task\n"
        "`todo done` — mark a task as done\n"
        "`todo del` — delete a task\n\n"

        "💬 *General*\n"
        "Just type naturally for everything else —\n"
        "Corti will figure out what you mean.\n\n"

        "🎙️ Voice messages are supported for all commands."
    )
    await update.message.reply_text(menu_text, parse_mode="Markdown")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ALLOWED_USER_ID:
        return

    name = context.user_data.get("name")
    if name:
        await update.message.reply_text(f"Welcome back, {name}! 👋")
    else:
        context.user_data["awaiting"] = "name"
        await update.message.reply_text("Hi! What should I call you?")

def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    os.makedirs("data", exist_ok=True)
    persistence = PicklePersistence(filepath="data/user_data.pickle")

    async def post_init(app):
    # Clear any stale conversation state from previous session
        for user_data in app.user_data.values():
            user_data.pop("edit_state", None)
            user_data.pop("edit_event", None)
            user_data.pop("edit_candidates", None)
            user_data.pop("edit_title", None)
            user_data.pop("edit_field", None)
            user_data.pop("pending_intent", None)
            user_data.pop("pending_params", None)
            user_data.pop("pending_missing_field", None)
            user_data.pop("delete_state", None)
            user_data.pop("delete_event", None)
            user_data.pop("delete_candidates", None)
            user_data.pop("delete_title", None)
            user_data.pop("notion_pending", None)
            user_data.pop("notion_parent_candidates", None)
            user_data.pop("todoist_action", None)
            user_data.pop("todoist_task", None)
            user_data.pop("todoist_candidates", None)
            user_data.pop("todoist_state", None)

    app = (
        Application.builder()
        .token(token)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CallbackQueryHandler(handle_cancel))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
    

    logger.info("Corti is running")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()