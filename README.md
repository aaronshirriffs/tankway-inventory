# MDR Inventory API — Internal Reference Guide

## 1. What it is

One Flask app that serves **two completely separate surfaces**, split by hostname:

| Surface | URL | Who uses it | Auth |
|---------|-----|-------------|------|
| **Admin Panel** | `https://tools.tankway.co.nz/inventory/` | MDR admins | Login (username + password) |
| **Public API** | `https://api.mdrlighting.co.nz/v1/...` | External customers / integrations | API key |

It reads live product and stock data from **Odoo** and exposes a curated, per-customer slice of it as JSON. The admin panel is invisible on the API domain — requesting `/inventory/...` on `api.mdrlighting.co.nz` returns 404 by design, so customers can never see the admin side.

---

## 2. How a request flows

```
Customer -> https://api.mdrlighting.co.nz  (nginx, TLS)
         -> 127.0.0.1:5004  (Flask app, inventory.service)
         -> Odoo (XML-RPC, system@ service account)
```

- **nginx** terminates HTTPS and reverse-proxies to the app on `127.0.0.1:5004`.
- The app itself only listens on localhost — never exposed directly to the internet.
- **Odoo** is the source of truth for products and stock; the app caches nothing — every `/v1/products` call queries Odoo live.

---

## 3. Admin Panel — day-to-day use

Go to **`https://tools.tankway.co.nz/inventory/`** and log in (must be an admin account in the shared platform `users.json`). From the dashboard you manage **API keys** — one key per customer/integration.

**For each key you control exactly what that customer sees:**

| Setting | What it does |
|---------|--------------|
| **Label** | A name to identify the key (e.g. "Acme Reseller") |
| **Allowed categories** | Which Odoo product categories are visible. **No categories = no products.** Sub-categories are included automatically. |
| **Excluded SKUs** | Specific product codes to hide, even if their category is allowed |
| **Warehouse mappings** | Group one or more Odoo warehouses under a friendly label (e.g. "Auckland"); the API reports combined stock per label |
| **Show price** | If on, includes `sales_price` in the response; if off, prices are hidden |
| **Expiry** | Optional date after which the key stops working |
| **Rate limits** | Per-minute, burst, and daily caps (see section 5) |

**Key actions:** Create, Update, Enable/Disable (toggle), Delete, view **Activity** (recent requests), and **Simulate** (preview the exact JSON a key would return — great for testing before handing a key to a customer).

> New API keys look like `mdr_xxxx...` and are generated automatically on create. Treat them like passwords.

---

## 4. Public API — for customers

### Authenticate
Pass the key either way:
- Header: `Authorization: Bearer mdr_xxxx`
- Or query string: `?key=mdr_xxxx`

### Endpoints

| Method & path | Purpose | Auth |
|---------------|---------|------|
| `GET /v1/products` | The customer's product + stock list | **Key required** |
| `GET /v1/status` | Liveness/health for clients | None |
| `GET /docs` | Human-readable API documentation | None |
| `GET /health` | Internal health check | None |

### Example

```bash
curl -H "Authorization: Bearer mdr_xxxx" \
     https://api.mdrlighting.co.nz/v1/products
```

### Response shape

```json
{
  "count": 2,
  "products": [
    {
      "id": 1234,
      "name": "Maestro DMX Controller",
      "sku": "0011767",
      "last_updated": "2026-06-01 09:12:33",
      "stock": { "Auckland": 12.0, "Wellington": 3.0 },
      "sales_price": 499.0
    }
  ]
}
```

- `stock` keys are **your warehouse-mapping labels**, with quantities summed across the mapped warehouses.
- `sales_price` appears **only if "Show price" is enabled** for the key.

### Error responses

| Code | Meaning |
|------|---------|
| `401` | Missing, invalid, disabled, or expired key |
| `429` | Rate limit exceeded — includes `retry_after_seconds` |
| `500` | Internal error (e.g. Odoo query failed) |

---

## 5. Rate limiting (per key)

Three independent limits, all set in the admin panel:

- **`rate_limit_per_minute`** — sustained steady rate (default 60).
- **`burst_allowance`** — extra requests tolerated in a short spike (default 20). Token bucket: capacity = per-minute + burst.
- **`rate_limit_daily`** — hard cap per NZ day (default 10,000); resets at NZ midnight.

When exceeded, the API returns **429** with `retry_after_seconds`. Limits are tracked **in memory per key** (not per IP) and **reset on service restart** — by design for this scale.

---

## 6. Files & config (on the server)

Location: **`/root/inventory/`**

| File | Role |
|------|------|
| `app.py` | The Flask app (routes for both surfaces) |
| `odoo_client.py` | Odoo XML-RPC integration |
| `storage.py` | Loads/saves API keys; in-memory activity log |
| `rate_limit.py` | Token-bucket rate limiter |
| `keys.json` | **Source of truth for all API keys** (atomic writes). Starts as `{}` |
| `.env` | Secrets: `SECRET_KEY`, `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_API_KEY` |
| `templates/` | `admin.html`, `login.html`, `docs.html` |
| `venv/` | Python virtual environment |

> Admin logins come from the shared platform file `/var/www/tankway/users.json` (only accounts flagged `admin: true` can log in).

---

## 7. Operations cheat-sheet

Run on the server (`ssh root@170.64.166.105`):

```bash
# Service control
systemctl status inventory.service
systemctl restart inventory.service     # e.g. after editing .env or code
systemctl is-enabled inventory.service  # starts on boot

# Live logs
journalctl -u inventory.service -f

# Quick health checks
curl https://api.mdrlighting.co.nz/v1/status
curl https://api.mdrlighting.co.nz/health

# nginx
nginx -t                                # validate config
systemctl reload nginx                  # apply config changes

# SSL (auto-renews; to check / force)
certbot certificates
certbot renew --dry-run
```

**Backups:** the only stateful file that is *not* in git is **`keys.json`** (it holds customer API tokens, so it is deliberately gitignored). Grab it via **Settings → Download config backup** and keep a copy off this server. See §9.

---

## 8. Security notes

- The app binds to **localhost only**; all public traffic goes through nginx + TLS.
- The admin domain is never referenced in any API response, header, or error — the two surfaces are firewalled by hostname.
- Odoo credentials live only in `.env` and never reach the public surface.
- **TLS:** Let's Encrypt cert for `api.mdrlighting.co.nz` is installed and **auto-renews** (expires 2026-09-04; Certbot timer handles renewal).
- API keys are bearer secrets — distribute over secure channels, and **disable rather than delete** if you want to keep a key's history/activity available until you're sure.

---

## 9. Rebuild from scratch (disaster recovery)

What restores from where:

| Piece | Source |
|---|---|
| Application code + templates + global settings JSONs | **this GitHub repo** |
| systemd unit, nginx config | **`deploy/`** in this repo |
| Required env var *names* | **`.env.example`** |
| `keys.json` (customer tokens + per-customer config) | **config backup** — Settings → Download config backup. *Not in git.* |
| Odoo API key, `TANKWAY_SECRET_KEY` | your password manager |
| `gmail_token.json` (email export) | re-mint with `auth_setup_export.py` |

**Steps**

```bash
# 1. Code
apt install -y python3-venv nginx
git clone git@github.com:aaronshirriffs/tankway-inventory.git /root/inventory
cd /root/inventory && python3 -m venv venv && venv/bin/pip install -r requirements.txt

# 2. Secrets
cp .env.example .env && chmod 600 .env     # fill in the Odoo values
#    TANKWAY_SECRET_KEY lives in /etc/tankway/shared.env (shared with the hub)

# 3. Restore customer keys (from the config backup .tar.gz)
tar xzf mdr-inventory-config-YYYYMMDD-HHMM.tar.gz -C /root/inventory

# 4. Service
cp deploy/inventory.service /etc/systemd/system/
mkdir -p /etc/systemd/system/inventory.service.d
cp deploy/inventory.service.d-gunicorn.conf /etc/systemd/system/inventory.service.d/gunicorn.conf
systemctl daemon-reload && systemctl enable --now inventory.service

# 5. nginx + TLS
cp deploy/nginx-mdr-api.conf /etc/nginx/sites-available/mdr-api
ln -s /etc/nginx/sites-available/mdr-api /etc/nginx/sites-enabled/
#    paste deploy/nginx-inventory-location.conf into the tools.tankway.co.nz server block
nginx -t && systemctl reload nginx
certbot --nginx -d api.mdrlighting.co.nz    # only once DNS points at the new server

# 6. Verify
curl https://api.mdrlighting.co.nz/v1/status
```

**Without the config backup** the app still runs, but every customer key and its
configuration is gone — you would have to reissue tokens to all customers. Take a
config backup whenever you add or materially change a key.

> Whole-server recovery (OS, other apps, certs) is *not* covered by this repo —
> that is what DigitalOcean droplet backups/snapshots are for. This repo restores
> **this application**.
