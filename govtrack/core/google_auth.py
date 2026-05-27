import os
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv
from govtrack.core.paths import CREDENTIALS_PATH as DEFAULT_CREDS_PATH
from govtrack.core.paths import ENV_PATH, TOKEN_PATH as DEFAULT_TOKEN_PATH

load_dotenv(ENV_PATH)

SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/calendar.readonly",
]

CREDS_PATH = os.getenv("CREDENTIALS_PATH", str(DEFAULT_CREDS_PATH))
TOKEN_PATH  = os.getenv("TOKEN_PATH", str(DEFAULT_TOKEN_PATH))

if CREDS_PATH == "credentials.json":
    CREDS_PATH = str(DEFAULT_CREDS_PATH)
if TOKEN_PATH == "token.json":
    TOKEN_PATH = str(DEFAULT_TOKEN_PATH)


def get_creds():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                try:
                    os.remove(TOKEN_PATH)
                except OSError:
                    pass
                creds = None
        if not creds or not creds.valid:
            flow  = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
            flow.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
            auth_url, _ = flow.authorization_url(prompt='consent')
            print(f'Open this URL in your browser:\n{auth_url}')
            code = input('Paste the authorization code here: ')
            flow.fetch_token(code=code)
            creds = flow.credentials
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds


def gmail_service():
    return build("gmail", "v1", credentials=get_creds())


def calendar_service():
    return build("calendar", "v3", credentials=get_creds())
