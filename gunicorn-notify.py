#!/usr/bin/env python3
"""Send a migration-result email IF /root/inventory/.notify.env is present.
No config file => silently does nothing (the result is always in the log).
.notify.env format (KEY=VALUE per line):
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=you@example.com
    SMTP_PASS=app-password-here
    MAIL_TO=you@example.com
    MAIL_FROM=you@example.com   # optional, defaults to SMTP_USER
"""
import os, ssl, smtplib, sys
from email.message import EmailMessage

CFG_PATH = "/root/inventory/.notify.env"
LOG_PATH = "/root/inventory/gunicorn-migrate.log"

def main():
    if not os.path.exists(CFG_PATH):
        print("notify: no .notify.env, skipping email")
        return
    cfg = {}
    for line in open(CFG_PATH):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    need = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_TO"]
    if not all(k in cfg for k in need):
        print("notify: .notify.env incomplete, skipping email")
        return

    status = sys.argv[1] if len(sys.argv) > 1 else "UNKNOWN"
    try:
        tail = open(LOG_PATH).read()[-3000:]
    except Exception:
        tail = "(log unavailable)"

    msg = EmailMessage()
    msg["Subject"] = f"[MDR Inventory API] gunicorn migration: {status}"
    msg["From"] = cfg.get("MAIL_FROM", cfg["SMTP_USER"])
    msg["To"] = cfg["MAIL_TO"]
    msg.set_content(
        f"gunicorn migration finished with status: {status}\n\n"
        f"PASS = now running on gunicorn.\n"
        f"ROLLED_BACK = stayed on the Flask dev server, site still up.\n"
        f"ROLLED_BACK_RECOVERY_FAILED = needs manual attention.\n\n"
        f"--- log tail ---\n{tail}\n"
    )
    ctx = ssl.create_default_context()
    with smtplib.SMTP(cfg["SMTP_HOST"], int(cfg["SMTP_PORT"]), timeout=30) as s:
        s.starttls(context=ctx)
        s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
        s.send_message(msg)
    print(f"notify: emailed {cfg['MAIL_TO']} (status={status})")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"notify: email failed: {e}")
