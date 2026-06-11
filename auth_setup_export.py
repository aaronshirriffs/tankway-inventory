"""
One-time Google OAuth setup for the email-export sender.

Creates gmail_token.json for the account you log in with (use
system@mdrlighting.co.nz), scoped to gmail.send ONLY — the token cannot read
mail.

Prereqs (Google Cloud Console, a LIVE project — e.g. the supplier portal's):
  1. APIs & Services -> Credentials -> Create OAuth client ID, type
     "Desktop app". Download the JSON as /root/inventory/credentials.json.
  2. Gmail API enabled on that project.
  3. system@mdrlighting.co.nz added as a Test User on the consent screen
     (unless the app is published).

Run ON THE DROPLET with an SSH port-forward so the browser redirect lands here:

  (laptop)   ssh -L 8765:localhost:8765 tankway
  (droplet)  cd /root/inventory && venv/bin/python auth_setup_export.py

Open the printed URL in your laptop browser, sign in as
system@mdrlighting.co.nz, approve.

NOTE 2026-06-12: this originally borrowed the assistant's OAuth client, but
that client's project (768116862550) was deleted, killing every token issued
by it. A credentials.json from a live project is now required.
"""
import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS = os.path.join(HERE, "credentials.json")
OUT = os.path.join(HERE, "gmail_token.json")
PORT = 8765


def main():
    if not os.path.exists(CREDENTIALS):
        raise SystemExit(
            f"{CREDENTIALS} not found.\nCreate a 'Desktop app' OAuth client in a "
            "live Google Cloud project (see this file's docstring) and download "
            "its JSON to that path, then re-run.")
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
    creds = flow.run_local_server(port=PORT, open_browser=False,
                                  authorization_prompt_message="Open this URL in your laptop browser:\n{url}")
    with open(OUT, "w") as f:
        f.write(creds.to_json())
    os.chmod(OUT, 0o600)
    print(f"Written {OUT} — exports will send from the account you just approved.")


if __name__ == "__main__":
    main()
