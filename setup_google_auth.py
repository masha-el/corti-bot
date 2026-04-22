import os
import pickle

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]

TOKEN_PATH = "credentials/google_token.pickle"
CREDENTIALS_PATH = "credentials/google_credentials.json"

def main():
    os.makedirs("credentials", exist_ok=True)
    creds = None

    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())  # silently gets a new access token with refresh token
        else: # full browser login flow.
            if not os.path.exists(CREDENTIALS_PATH):
                print(f"ERROR: {CREDENTIALS_PATH} not found.")
                print("Download OAuth credentials JSON from Google Cloud Console.")
                return
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES) # preparing the flow
            creds = flow.run_local_server(port=0) # pick ANY port automatically and open browser

        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)

    print(f"Token saved to {TOKEN_PATH}")
    print(f"Next step — upload to GCP VM:")
    print(f"  scp {TOKEN_PATH} <user>@<vm-ip>:~/corti-bot/credentials/google_token.pickle")

if __name__ == "__main__":
    main()