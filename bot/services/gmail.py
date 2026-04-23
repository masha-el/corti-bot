import os
import base64
import pickle
import logging
from email.mime.text import MIMEText

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

logger = logging.getLogger(__name__)

TOKEN_PATH = "credentials/google_token.pickle"

def _get_service():
    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError("Google token not found at credentials/google_token.pickle")
    
    with open(TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)
        
    return build("gmail", "v1", credentials=creds)

def read_emails(query: str = "", max_results: int = 5, unread_only: bool = True) -> list[dict]:
    service = _get_service()
    # Build search query
    if query and unread_only:
        search_query = f"is:unread {query}"
    elif query:
        search_query = query
    elif unread_only:
        search_query = "is:unread"
    else:
        search_query = ""  #empty query = all emails, sorted by date
    
    # cap at 5
    max_results = min(max_results, 5)
    
    results = service.users().messages().list(
        userId="me",
        q=search_query,
        maxResults=max_results,
    ).execute()

    messages = results.get("messages", [])
    emails = []

    for msg in messages:
        msg_data = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()

        headers = {h["name"]: h["value"] for h in msg_data["payload"]["headers"]}
        emails.append({
            "id": msg["id"],
            "from": headers.get("From", "Unknown"),
            "subject": headers.get("Subject", "No subject"),
            "date": headers.get("Date", ""),
            "snippet": msg_data.get("snippet", "")[:200],
        })
    
    return emails

def get_email_body(message_id: str) -> str:
    """Fetches the full plain text body of a specific email."""
    service = _get_service()

    msg = service.users().messages().get(
        userId="me",
        id=message_id,
        format="full",
    ).execute()

    payload = msg["payload"]

    # Simple email — body directly in payload
    if "body" in payload and payload["body"].get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8")
    
    # Multipart email — find the text/plain part
    parts = payload.get("parts", [])
    for part in parts:
        if part["mimeType"] == "text/plain" and part["body"].get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8")

    return "Could not extract email body."

def send_email(to: str, subject: str, body: str) -> None:
    service = _get_service()
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(
        userId="me",
        body={"raw": raw}
    ).execute()

def trash_email(message_id: str) -> None:
    service = _get_service()
    service.users().messages().trash(userId="me", id=message_id).execute()

def get_email_sender(message_id: str) -> dict:
    """Returns From address and Subject for reply/forward prefilling."""
    service = _get_service()
    msg = service.users().messages().get(
        userId="me",
        id=message_id,
        format="metadata",
        metadataHeaders=["From", "Subject", "Reply-To"],
    ).execute()

    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    return {
        "from": headers.get("From", ""),
        "reply_to": headers.get("Reply-To", headers.get("From", "")),
        "subject": headers.get("Subject", ""),
    }
