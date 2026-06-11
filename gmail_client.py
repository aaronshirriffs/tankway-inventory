"""
Gmail sender for scheduled stock exports.

Same Gmail-API pattern as the other tools on this server (supplier portal /
assistant), but least-privilege: the token is scoped to gmail.send only — it
cannot read any mailbox. The token file is self-contained (client id/secret
embedded, as written by google-auth's creds.to_json()), created once by
auth_setup_export.py with the system@mdrlighting.co.nz account.
"""
import base64
import json
import os
import threading
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

TOKEN_PATH = os.path.join(os.path.dirname(__file__), "gmail_token.json")

MIMETYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
}

_refresh_lock = threading.Lock()


def is_authorised():
    return os.path.exists(TOKEN_PATH)


def _get_service():
    if not is_authorised():
        raise RuntimeError(
            "Gmail is not connected — run auth_setup_export.py once as "
            "system@mdrlighting.co.nz to create gmail_token.json.")
    with _refresh_lock:
        creds = Credentials.from_authorized_user_file(TOKEN_PATH)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            tmp = TOKEN_PATH + ".tmp"
            with open(tmp, "w") as f:
                f.write(creds.to_json())
            os.replace(tmp, TOKEN_PATH)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def sender_address():
    """The connected account's address, or None if not connected/reachable."""
    try:
        service = _get_service()
        return service.users().getProfile(userId="me").execute().get("emailAddress")
    except Exception:
        return None


def send_with_attachment(to, subject, body, filename, data, fmt, reply_to=None):
    """Send `data` (bytes) as an attachment. `to` may be comma-separated."""
    service = _get_service()

    msg = MIMEMultipart()
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.attach(MIMEText(body))

    part = MIMEBase(*MIMETYPES.get(fmt, "application/octet-stream").split("/", 1))
    part.set_payload(data)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
