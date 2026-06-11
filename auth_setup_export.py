"""
One-time Google OAuth setup for the email-export sender.

Creates gmail_token.json for the account you log in with (use
system@mdrlighting.co.nz), scoped to gmail.send ONLY — the token cannot read
mail. Reuses the Desktop-app OAuth client from the voice assistant's token
(same Google Cloud project), so no credentials.json download is needed.

Run ON THE DROPLET with an SSH port-forward so the browser redirect lands here:

  (laptop)   ssh -L 8765:localhost:8765 tankway
  (droplet)  cd /root/inventory && venv/bin/python auth_setup_export.py

Open the printed URL in your laptop browser, sign in as
system@mdrlighting.co.nz, approve. If Google says the app is unverified /
access blocked, add system@mdrlighting.co.nz as a Test User on the OAuth
consent screen in Google Cloud Console first.
"""
import json
import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
ASSISTANT_TOKEN = "/root/assistant/token.json"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gmail_token.json")
PORT = 8765


def main():
    src = json.load(open(ASSISTANT_TOKEN))
    client_config = {
        "installed": {
            "client_id": src["client_id"],
            "client_secret": src["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=PORT, open_browser=False,
                                  authorization_prompt_message="Open this URL in your laptop browser:\n{url}")
    with open(OUT, "w") as f:
        f.write(creds.to_json())
    os.chmod(OUT, 0o600)
    print(f"Written {OUT} — exports will send from the account you just approved.")


if __name__ == "__main__":
    main()
