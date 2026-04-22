import os
import pickle
import logging
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

logger = logging.getLogger(__name__)

TOKEN_PATH = "credentials/google_token.pickle"
TIMEZONE = "Asia/Jerusalem"
ISRAEL_OFFSET = timezone(timedelta(hours=3))

def _get_service():
    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError("Google token not found at credentials/google_token.pickle")
    
    with open(TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)

    return build("calendar", "v3", credentials=creds)

def _parse_date_range(date_str: str, period: str = "day") -> tuple[datetime, datetime]:
    now = datetime.now(tz=ISRAEL_OFFSET)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # days_since_sunday: Sunday=0, Monday=1 ... Saturday=6
    days_since_sunday = (today.weekday() + 1) % 7
    this_sunday = today - timedelta(days=days_since_sunday)

    if date_str == "today":
        return today, today + timedelta(days=1)

    elif date_str == "tomorrow":
        tomorrow = today + timedelta(days=1)
        return tomorrow, tomorrow + timedelta(days=1)

    elif date_str == "this week":
        return this_sunday, this_sunday + timedelta(days=7)

    elif date_str == "next week":
        next_sunday = this_sunday + timedelta(days=7)
        return next_sunday, next_sunday + timedelta(days=7)

    else:
        try:
            base = datetime.strptime(date_str, "%d-%m-%Y")
            base = base.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=ISRAEL_OFFSET)
        except ValueError:
            base = today
        return base, base + timedelta(days=1)

def _find_event_id(service, title: str, date: str) -> list[dict]:
    """Returns list of matching events with id and title."""
    time_min, time_max = _parse_date_range(date, "day")

    result = service.events().list(
        calendarId="primary",
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    matches = []
    for e in result.get("items", []):
        if title.lower() in e.get("summary", "").lower():
            matches.append({
                "id": e["id"],
                "title": e.get("summary", ""),
                "start": e["start"].get("dateTime", e["start"].get("date", "")),
            })
    return matches

def find_event(title: str, date: str) -> dict | None:
    """
    Returns:
        - Single event dict if exactly one match
        - List of event dicts if multiple matches
        - None if no matches
    """
    service = _get_service()
    time_min, time_max = _parse_date_range(date, "day")

    result = service.events().list(
        calendarId="primary",
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    matches = [
        e for e in result.get("items", [])
        if title.lower() in e.get("summary", "").lower()
    ]

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return matches

def update_event_field(event: dict, field: str, new_value: str) -> str:
    """
    Updates a single field on an existing event object and saves it.
    field: "title" | "date" | "time" | "duration"
    Returns a confirmation string.
    """
    service = _get_service()

    if field == "title":
        event["summary"] = new_value

    elif field == "date":
        current_start = datetime.fromisoformat(event["start"]["dateTime"])
        new_start = datetime.strptime(
            f"{new_value} {current_start.strftime('%H:%M')}",
            "%d-%m-%Y %H:%M"
        )
        current_end = datetime.fromisoformat(event["end"]["dateTime"])
        duration = current_end - current_start
        event["start"] = {"dateTime": new_start.isoformat(), "timeZone": TIMEZONE}
        event["end"]   = {"dateTime": (new_start + duration).isoformat(), "timeZone": TIMEZONE}

    elif field == "time":
        current_start = datetime.fromisoformat(event["start"]["dateTime"])
        new_start = datetime.strptime(
            f"{current_start.strftime('%d-%m-%Y')} {new_value}",
            "%d-%m-%Y %H:%M"
        )
        current_end = datetime.fromisoformat(event["end"]["dateTime"])
        duration = current_end - current_start
        event["start"] = {"dateTime": new_start.isoformat(), "timeZone": TIMEZONE}
        event["end"]   = {"dateTime": (new_start + duration).isoformat(), "timeZone": TIMEZONE}

    elif field == "duration":
        current_start = datetime.fromisoformat(event["start"]["dateTime"])
        new_end = current_start + timedelta(minutes=int(new_value))
        event["end"] = {"dateTime": new_end.isoformat(), "timeZone": TIMEZONE}

    service.events().update(
        calendarId="primary",
        eventId=event["id"],
        body=event,
    ).execute()

    return event.get("summary", "event")

def read_events(date_str: str, period: str = "day") -> list[dict]:
    service = _get_service()
    time_min, time_max = _parse_date_range(date_str, period)

    result = service.events().list(
        calendarId="primary",
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = []
    for e in result.get("items", []):
        start = e["start"].get("dateTime", e["start"].get("date", ""))
        events.append({
            "title": e.get("summary", "No title"),
            "start": start,
            "location": e.get("location", ""),
        })
    return events

def create_event(title: str, date: str, time: str, duration_minutes: int = 60) -> None:
    service = _get_service()

    start_dt = datetime.strptime(f"{date} {time}", "%d-%m-%Y %H:%M")
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    event = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end":   {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
    }

    service.events().insert(calendarId="primary", body=event).execute()

def delete_event_by_id(event_id: str) -> None:
    service = _get_service()
    service.events().delete(calendarId="primary", eventId=event_id).execute()

def edit_event(title: str, date: str, new_title: str = None, new_date: str = None, 
               new_time: str = None, new_duration_minutes: int = None) -> str:
    service = _get_service()
    matches = _find_event_id(service, title, date)

    if not matches:
        return f"❌ No event found matching '{title}' on {date}."
    
    if len(matches) > 1:
        titles = "\n".join([f"• {m['title']} at {m['start']}" for m in matches])
        return f"Found multiple events — be more specific:\n{titles}"

    event = service.events().get(calendarId="primary", eventId=matches[0]["id"]).execute()

    # Only update fields the user explicitly changed
    if new_title:
        event["summary"] = new_title

    if new_date or new_time:
        current_start = datetime.fromisoformat(event["start"]["dateTime"])
        start_date = new_date if new_date else current_start.strftime("%d-%m-%Y")
        start_time = new_time if new_time else current_start.strftime("%H:%M")
        new_start = datetime.strptime(f"{start_date} {start_time}", "%d-%m-%Y %H:%M")

        if new_duration_minutes:
            new_end = new_start + timedelta(minutes=new_duration_minutes)
        else:
            current_end = datetime.fromisoformat(event["end"]["dateTime"])
            duration = current_end - current_start
            new_end = new_start + duration

        event["start"] = {"dateTime": new_start.isoformat(), "timeZone": TIMEZONE}
        event["end"]   = {"dateTime": new_end.isoformat(),   "timeZone": TIMEZONE}
        
    service.events().update(calendarId="primary", eventId=matches[0]["id"], body=event).execute()
    return f"✅ Updated: *{event['summary']}*"

def get_raw_events(date_str: str, period: str = "day") -> list[dict]:
    """Returns raw Google API event objects — used when we need to act on events later."""
    service = _get_service()
    time_min, time_max = _parse_date_range(date_str, period)

    result = service.events().list(
        calendarId="primary",
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    return result.get("items", [])