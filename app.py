"""
MDR Inventory — two surfaces in one Flask app, separated by hostname:

  1. ADMIN PANEL   tools.tankway.co.nz/inventory/   (Flask-Login, admins only)
  2. PUBLIC API    api.mdrlighting.co.nz/v1/...      (API-key auth)

The public surface never references the admin domain in any response, header,
or error. Admin routes are hard-blocked on any host other than the admin host.
"""
import os
import time
from datetime import datetime, date, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import (
    Flask, request, jsonify, redirect, url_for, render_template,
    render_template_string, abort, send_from_directory, send_file,
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user,
)

import odoo_client
import storage
import rate_limit
import docgen

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Surfaces / config
# ---------------------------------------------------------------------------
ADMIN_HOST = "tools.tankway.co.nz"
API_HOST = "api.mdrlighting.co.nz"
USERS_FILE = "/var/www/tankway/users.json"   # shared platform users (Flask-Login)
NZT = ZoneInfo("Pacific/Auckland")
API_NAME = "MDR Inventory API"

app = Flask(__name__)
# Single sign-on with the main Tankway Tools hub: share its session secret and the
# default session cookie ("session" at path "/") so a user already signed in to
# tools.tankway.co.nz flows straight into this tool without a second login.
# The secret is defined once in /etc/tankway/shared.env (read by both services).
app.secret_key = os.environ.get("TANKWAY_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("TANKWAY_SECRET_KEY is not set — define it in /etc/tankway/shared.env")
# Preserve the field order we build into JSON responses (don't alphabetise keys).
app.json.sort_keys = False

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


@app.template_filter("nzdatetime")
def nzdatetime(value):
    """Render a stored UTC ISO timestamp in NZ local time, e.g. '11:41am, 6th June 2026'.
    Returns 'never' for an empty value (so unused keys read cleanly)."""
    if not value:
        return "never"
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(NZT)
    hour12 = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    day = dt.day
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{hour12}:{dt.minute:02d}{ampm}, {day}{suffix} {dt.strftime('%B %Y')}"


@login_manager.unauthorized_handler
def _unauthorized():
    # Not signed in to Tankway Tools — send to the main hub login, not a separate one.
    return redirect("/")


# ---------------------------------------------------------------------------
# Auth (shared users.json pattern, admins only)
# ---------------------------------------------------------------------------
import json


def load_platform_users():
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def is_admin(username):
    return load_platform_users().get(username, {}).get("admin", False)


class User(UserMixin):
    def __init__(self, username):
        self.id = username


@login_manager.user_loader
def load_user(username):
    return User(username) if username in load_platform_users() else None


def tools_required(f):
    """Tier 1 — any authenticated Tankway Tools user (admin layer not required)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.host.split(":")[0] != ADMIN_HOST:
            abort(404)
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Tier 2 — authenticated AND admin. Non-admins get a styled 403 page."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.host.split(":")[0] != ADMIN_HOST:
            abort(404)
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not is_admin(current_user.id):
            return render_template("forbidden.html", username=current_user.id), 403
        return f(*args, **kwargs)
    return wrapper


@app.context_processor
def _inject_role():
    """Expose `is_admin_user` to every template for sidebar/role-aware rendering."""
    try:
        return {"is_admin_user": current_user.is_authenticated and is_admin(current_user.id)}
    except Exception:
        return {"is_admin_user": False}


# ---------------------------------------------------------------------------
# Host guard: admin paths are invisible on any non-admin host
# ---------------------------------------------------------------------------
@app.before_request
def _host_guard():
    host = request.host.split(":")[0]
    if request.path.startswith("/inventory") and host != ADMIN_HOST:
        abort(404)


# ---------------------------------------------------------------------------
# Shared product-building logic (used by /v1/products and admin "simulate")
# ---------------------------------------------------------------------------
def build_products(config):
    """Return the exact customer-facing product list for a key config."""
    uid, models = odoo_client.connect()

    category_ids = [c["id"] for c in config.get("allowed_categories", [])]
    excluded = config.get("excluded_skus", [])
    excluded_category_ids = [c["id"] for c in config.get("excluded_categories", [])]
    products = odoo_client.fetch_products(models, uid, category_ids, excluded, excluded_category_ids)
    product_ids = [p["id"] for p in products]

    mappings = config.get("warehouse_mappings", [])
    # Stock sources are either whole warehouses (kind 'wh', the default) or
    # individual stock locations such as EXT/Stock/AUS/Showtools (kind 'loc').
    needed_wh = {w["id"] for m in mappings for w in m.get("warehouses", []) if w.get("kind") != "loc"}
    needed_loc = {w["id"] for m in mappings for w in m.get("warehouses", []) if w.get("kind") == "loc"}
    stock_wh = odoo_client.stock_by_warehouse(models, uid, product_ids, list(needed_wh))
    stock_loc = odoo_client.stock_by_location(models, uid, product_ids, list(needed_loc))

    def _qty(src, pid):
        store = stock_loc if src.get("kind") == "loc" else stock_wh
        return store.get(src["id"], {}).get(pid, 0.0)

    # Lead times come from the global Warehouses settings, keyed by source. They are
    # free text (e.g. "5-7"); a label that combines several sources reports the longest
    # (highest-magnitude) lead time among them, displayed as "5-7 days".
    wh_settings = storage.load_warehouse_settings()

    def _src_lead_raw(src):
        skey = "%s:%s" % (src.get("kind", "wh"), src["id"])
        return storage.lead_time_value(wh_settings.get(skey, {}))

    mapping_lead = {}  # label -> (rank, display_text)
    for m in mappings:
        best = (-2, "")
        for w in m.get("warehouses", []):
            raw = _src_lead_raw(w)
            r = storage.lead_time_rank(raw)
            if r > best[0]:
                best = (r, storage.lead_time_display(raw))
        prev = mapping_lead.get(m["label"])
        if prev is None or best[0] > prev[0]:
            mapping_lead[m["label"]] = best

    # Incoming / forecast stock (per-key opt-in). Map a destination warehouse id
    # to the customer-facing label used for that warehouse; fall back to "Incoming".
    show_incoming = bool(config.get("show_incoming"))
    incoming_map = {}
    wh_to_label = {}
    if show_incoming:
        incoming_map = odoo_client.get_incoming_stock(models, uid, product_ids)
        for m in mappings:
            for w in m.get("warehouses", []):
                if w.get("kind", "wh") == "wh":
                    wh_to_label.setdefault(w["id"], m["label"])

    show_price = bool(config.get("show_price"))

    # Customer buy price (per-key pricelist). Computed once for all products at qty 1.
    pricelist = config.get("pricelist") or None
    buy_prices = {}
    if pricelist and pricelist.get("id"):
        buy_prices = odoo_client.pricelist_prices(models, uid, pricelist["id"], product_ids, qty=1)

    def _incoming_label(wid):
        # Prefer a global per-warehouse "incoming label" (Warehouses page); otherwise
        # reuse that warehouse's customer-facing stock label; otherwise "Incoming".
        if wid is not None:
            gl = ((wh_settings.get("wh:%s" % wid, {}) or {}).get("incoming_label") or "").strip()
            if gl:
                return gl
        return wh_to_label.get(wid, "Incoming")

    out = []
    for p in products:
        stock_obj = {}
        for m in mappings:
            label = m["label"]
            total = sum(_qty(w, p["id"]) for w in m.get("warehouses", []))
            stock_obj[label] = round(stock_obj.get(label, 0.0) + total, 2)
        availability = [
            {"label": lbl, "qty": qty, "lead_time": mapping_lead.get(lbl, (-2, ""))[1]}
            for lbl, qty in stock_obj.items()
        ]
        # Field order here is the customer-facing layout (JSON key order is preserved).
        item = {
            "id": p["id"],
            "name": p["name"],
            "sku": p.get("default_code") or None,
            "last_updated": p.get("write_date"),
        }
        if show_price:
            item["sales_price"] = round(p.get("list_price") or 0.0, 2)
        if pricelist and pricelist.get("id"):
            item["your_price"] = buy_prices.get(p["id"], round(p.get("list_price") or 0.0, 2))
        item["stock"] = stock_obj
        item["availability"] = availability
        if show_incoming:
            item["incoming"] = [
                {
                    "quantity": sh["quantity"],
                    "expected_date": sh["expected_date"],
                    "label": _incoming_label(sh.get("warehouse_id")),
                }
                for sh in incoming_map.get(p["id"], [])
            ]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Key validation / rate limiting helpers
# ---------------------------------------------------------------------------
def extract_key():
    k = request.args.get("key")
    if not k:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            k = auth[7:].strip()
    return k


def key_is_valid(config):
    if not config or not config.get("enabled"):
        return False
    expiry = config.get("expiry")
    if expiry:
        try:
            exp = date.fromisoformat(str(expiry)[:10])
            if datetime.now(NZT).date() > exp:
                return False
        except ValueError:
            return False
    return True


def _day_context():
    now = datetime.now(NZT)
    day_key = now.date().isoformat()
    nxt = datetime.combine(now.date(), datetime.min.time(), tzinfo=NZT)
    # seconds until next NZ midnight
    secs = 86400 - (now - nxt).total_seconds()
    return day_key, secs


def client_ip():
    return request.headers.get("X-Real-IP") or request.remote_addr or "-"


# ===========================================================================
# PUBLIC API  (api.mdrlighting.co.nz)
# ===========================================================================
@app.route("/v1/products")
def v1_products():
    token = extract_key()
    config = storage.get_key(token) if token else None
    if not key_is_valid(config):
        return jsonify({"error": "Invalid or expired API key"}), 401

    day_key, secs_reset = _day_context()
    allowed, retry = rate_limit.check(
        token,
        config.get("rate_limit_per_minute", 60),
        config.get("burst_allowance", 0),
        config.get("rate_limit_daily", 0),
        time.monotonic(), day_key, secs_reset,
    )
    if not allowed:
        storage.log_activity(token, client_ip(), 0, True)
        return jsonify({"error": "Rate limit exceeded", "retry_after_seconds": retry}), 429

    try:
        products = build_products(config)
    except Exception:
        return jsonify({"error": "Internal server error"}), 500

    storage.touch_last_used(token)
    storage.log_activity(token, client_ip(), len(products), False)
    return jsonify({"count": len(products), "products": products})


@app.route("/v1/status")
def v1_status():
    return jsonify({
        "api": API_NAME,
        "status": "ok",
        "timestamp": datetime.now(NZT).isoformat(timespec="seconds"),
    })


@app.route("/docs")
def docs():
    # Public API docs, generated from the live global config (labels, lead times,
    # default rate limits) so they stay in sync with the admin settings.
    s = storage.load_settings()
    labels = docgen._customer_labels()  # [(label, lead_time_display), ...]
    ex = labels[:2] if labels else [("Available Immediately", ""), ("On Request", "5-7 days")]
    example = {
        "count": 1,
        "products": [{
            "id": 1423,
            "name": "Chauvet Maverick MK3 Spot",
            "sku": "CHV-MK3-SPOT",
            "last_updated": "2026-06-05 21:14:02",
            "sales_price": 4299.00,
            "your_price": 3869.10,
            "stock": {lbl: q for (lbl, _), q in zip(ex, [7, 3])},
            "availability": [{"label": lbl, "qty": q, "lead_time": lead}
                             for ((lbl, lead), q) in zip(ex, [7, 3])],
            "incoming": [{"quantity": 20, "expected_date": "2026-06-15",
                          "label": (ex[0][0] if ex else "Incoming")}],
        }],
    }
    html = render_template(
        "docs.html", api_host=API_HOST, api_name=API_NAME,
        rpm=s["default_rate_limit_per_minute"],
        rdaily=s["default_rate_limit_daily"],
        burst=s["default_burst_allowance"],
        labels=labels,
        example_json=docgen.price_json(example),
    )
    # Docs are generated live from config — never let browsers/proxies serve a stale copy.
    resp = app.make_response(html)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
def root():
    # Public surface root → point developers at the docs. No admin references.
    if request.host.split(":")[0] == ADMIN_HOST:
        abort(404)
    return redirect("/docs")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ===========================================================================
# ADMIN PANEL  (tools.tankway.co.nz/inventory/)
# ===========================================================================
@app.route("/inventory/login", methods=["GET", "POST"])
def login():
    if request.host.split(":")[0] != ADMIN_HOST:
        abort(404)
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").lower().strip()
        password = request.form.get("password", "")
        users = load_platform_users()
        stored = users.get(username, {}).get("password")
        if username in users and password == stored:
            # Any valid Tankway Tools user may sign in (Tier 1). Admin-only
            # sections are gated per-route by @admin_required.
            login_user(User(username))
            return redirect(url_for("admin_dashboard"))
        else:
            error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/inventory/logout")
def logout():
    logout_user()
    return redirect("/")  # back to the main Tankway Tools hub


@app.route("/inventory/")
@tools_required
def admin_dashboard():
    keys = storage.load_keys()
    odoo_ok, odoo_msg = True, ""
    categories, warehouses, locations, pricelists = [], [], [], []
    try:
        uid, models = odoo_client.connect()
        categories = odoo_client.list_categories(models, uid)
        warehouses = odoo_client.list_warehouses(models, uid)
        locations = odoo_client.list_locations(models, uid)
        pricelists = odoo_client.list_pricelists(models, uid)
    except Exception as e:
        odoo_ok, odoo_msg = False, "Odoo unavailable — category/warehouse lists could not load."

    # If an admin has curated a global "available categories" list, the key-form
    # pickers offer only those; otherwise they offer everything from Odoo.
    available = storage.load_available_categories()
    if available:
        allowed_ids = {c["id"] for c in available}
        form_categories = [c for c in categories if c["id"] in allowed_ids] or available
    else:
        form_categories = categories

    # Only stock sources marked "selectable" on the Warehouses page appear in the
    # per-key mapping picker (absent setting => selectable by default).
    wh_settings = storage.load_warehouse_settings()
    warehouses = [w for w in warehouses if wh_settings.get("wh:%s" % w["id"], {}).get("enabled", True)]
    locations = [l for l in locations if wh_settings.get("loc:%s" % l["id"], {}).get("enabled", True)]

    settings = storage.load_settings()
    rate_defaults = {
        "per_minute": settings["default_rate_limit_per_minute"],
        "daily": settings["default_rate_limit_daily"],
        "burst": settings["default_burst_allowance"],
    }
    return render_template(
        "admin.html",
        keys=keys, categories=form_categories, warehouses=warehouses, locations=locations,
        pricelists=pricelists,
        odoo_ok=odoo_ok, odoo_msg=odoo_msg, username=current_user.id, sidebar_active="keys",
        rate_defaults=rate_defaults,
    )


def _parse_key_form(form, warehouses, categories, locations=None, pricelists=None):
    """Build the stored key fields from posted form data."""
    wh_by_id = {w["id"]: w["name"] for w in warehouses}
    loc_by_id = {l["id"]: l["complete_name"] for l in (locations or [])}
    cat_by_id = {c["id"]: c["complete_name"] for c in categories}
    pl_by_id = {p["id"]: p["name"] for p in (pricelists or [])}

    pricelist = None
    pl_raw = (form.get("pricelist") or "").strip()
    if pl_raw:
        try:
            plid = int(pl_raw)
            pricelist = {"id": plid, "name": pl_by_id.get(plid, str(plid))}
        except ValueError:
            pricelist = None

    allowed_categories = []
    for cid in form.getlist("allowed_categories"):
        cid = int(cid)
        allowed_categories.append({"id": cid, "name": cat_by_id.get(cid, str(cid))})

    excluded_categories = []
    for cid in form.getlist("excluded_categories"):
        cid = int(cid)
        excluded_categories.append({"id": cid, "name": cat_by_id.get(cid, str(cid))})

    excluded_skus = [s.strip() for s in form.get("excluded_skus", "").replace(",", "\n").splitlines() if s.strip()]

    warehouse_mappings = []
    try:
        rows = json.loads(form.get("warehouse_mappings_json", "[]"))
    except ValueError:
        rows = []
    for r in rows:
        label = (r.get("label") or "").strip()
        # New format: sources=[{id, kind}]; legacy format: warehouse_ids=[id,...]
        sources = r.get("sources")
        if sources is None:
            sources = [{"id": w, "kind": "wh"} for w in r.get("warehouse_ids", [])]
        whs = []
        for s in sources:
            sid = int(s["id"])
            kind = s.get("kind", "wh")
            name = (loc_by_id.get(sid) if kind == "loc" else wh_by_id.get(sid)) or str(sid)
            whs.append({"id": sid, "name": name, "kind": kind})
        if not whs:
            continue
        if not label:
            # Blank customer label -> fall back to the source's global default label
            # (set on the Warehouses page), else the source's own name.
            s0 = whs[0]
            gl = (storage.load_warehouse_settings().get("%s:%s" % (s0["kind"], s0["id"]), {}) or {}).get("label")
            label = (gl or s0["name"] or "").strip() or "Stock"
        warehouse_mappings.append({"warehouses": whs, "label": label})

    settings = storage.load_settings()
    return {
        "label": form.get("label", "").strip() or "Unnamed",
        "expiry": (form.get("expiry") or "").strip() or None,
        "allowed_categories": allowed_categories,
        "excluded_categories": excluded_categories,
        "excluded_skus": excluded_skus,
        "warehouse_mappings": warehouse_mappings,
        "pricelist": pricelist,
        "show_price": form.get("show_price") == "on",
        "show_incoming": form.get("show_incoming") == "on",
        "rate_limit_per_minute": int(form.get("rate_limit_per_minute") or settings["default_rate_limit_per_minute"]),
        "rate_limit_daily": int(form.get("rate_limit_daily") or settings["default_rate_limit_daily"]),
        "burst_allowance": int(form.get("burst_allowance") or settings["default_burst_allowance"]),
    }


def _live_lists():
    uid, models = odoo_client.connect()
    return (
        odoo_client.list_categories(models, uid),
        odoo_client.list_warehouses(models, uid),
        odoo_client.list_locations(models, uid),
        odoo_client.list_pricelists(models, uid),
    )


@app.route("/inventory/keys/create", methods=["POST"])
@tools_required
def create_key():
    try:
        categories, warehouses, locations, pricelists = _live_lists()
    except Exception:
        categories, warehouses, locations, pricelists = [], [], [], []
    fields = _parse_key_form(request.form, warehouses, categories, locations, pricelists)
    storage.new_key(**fields)
    return redirect(url_for("admin_dashboard"))


@app.route("/inventory/keys/<token>/update", methods=["POST"])
@tools_required
def update_key(token):
    try:
        categories, warehouses, locations, pricelists = _live_lists()
    except Exception:
        categories, warehouses, locations, pricelists = [], [], [], []
    fields = _parse_key_form(request.form, warehouses, categories, locations, pricelists)
    storage.update_key(token, **fields)
    if request.headers.get("X-Requested-With"):
        # AJAX save — keep the editor open client-side; just confirm success.
        return jsonify({"ok": True, "label": fields["label"]})
    return redirect(url_for("admin_dashboard"))


@app.route("/inventory/keys/<token>/toggle", methods=["POST"])
@tools_required
def toggle_key(token):
    cfg = storage.get_key(token)
    if cfg:
        storage.update_key(token, enabled=not cfg.get("enabled"))
    return redirect(url_for("admin_dashboard"))


@app.route("/inventory/keys/<token>/delete", methods=["POST"])
@tools_required
def remove_key(token):
    storage.delete_key(token)
    rate_limit.reset(token)
    return redirect(url_for("admin_dashboard"))


@app.route("/inventory/keys/<token>/activity")
@tools_required
def key_activity(token):
    return jsonify({"activity": storage.get_activity(token)})


@app.route("/inventory/keys/<token>/simulate")
@tools_required
def simulate_key(token):
    config = storage.get_key(token)
    if not config:
        return jsonify({"error": "Key not found"}), 404
    try:
        products = build_products(config)
    except Exception as e:
        return jsonify({"error": f"Odoo query failed: {e}"}), 502
    return jsonify({"count": len(products), "products": products})


@app.route("/inventory/logo.png")
def inventory_logo():
    # Served locally so the admin chrome never depends on an external image.
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "mdr-logo.png")


@app.route("/inventory/status")
@tools_required
def admin_status():
    keys = storage.load_keys()
    odoo_ok, odoo_msg = odoo_client.test_connection()
    total = len(keys)
    active = sum(1 for k in keys.values() if k.get("enabled"))
    today = datetime.now(NZT).date().isoformat()
    req_today = 0
    rl_today = 0
    for tok in keys:
        for a in storage.get_activity(tok):
            if str(a.get("timestamp", ""))[:10] == today:
                req_today += 1
                if a.get("rate_limited"):
                    rl_today += 1
    return render_template(
        "status.html",
        username=current_user.id, sidebar_active="status",
        odoo_ok=odoo_ok, odoo_msg=odoo_msg,
        total=total, active=active, disabled=total - active,
        req_today=req_today, rl_today=rl_today,
        api_name=API_NAME, api_host=API_HOST,
        now=datetime.now(NZT).isoformat(timespec="seconds"),
    )


@app.route("/inventory/activity-log")
@tools_required
def admin_activity_log():
    keys = storage.load_keys()
    rows = []
    for tok, cfg in keys.items():
        for a in storage.get_activity(tok):
            rows.append({
                "timestamp": a.get("timestamp"),
                "label": cfg.get("label", "—"),
                "ip": a.get("ip"),
                "product_count": a.get("product_count"),
                "rate_limited": a.get("rate_limited"),
            })
    rows.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    return render_template(
        "activity_log.html",
        username=current_user.id, sidebar_active="activity", rows=rows,
    )


def _all_sources():
    """Combined stock sources for the global Warehouses page: warehouses + EXT locations."""
    uid, models = odoo_client.connect()
    sources = []
    for w in odoo_client.list_warehouses(models, uid):
        sources.append({"key": "wh:%s" % w["id"], "kind": "wh", "name": w["name"]})
    for l in odoo_client.list_locations(models, uid):
        sources.append({"key": "loc:%s" % l["id"], "kind": "loc", "name": l["complete_name"]})
    return sources


@app.route("/inventory/warehouses", methods=["GET", "POST"])
@admin_required
def admin_warehouses():
    odoo_ok, odoo_msg = True, ""
    try:
        sources = _all_sources()
    except Exception:
        sources, odoo_ok, odoo_msg = [], False, "Odoo unavailable — warehouse/location list could not load."

    if request.method == "POST":
        settings = {}
        for s in sources:
            label = (request.form.get("label__" + s["key"]) or "").strip()
            lead = (request.form.get("lead__" + s["key"]) or "").strip()
            incoming = (request.form.get("incoming__" + s["key"]) or "").strip()
            enabled = request.form.get("enabled__" + s["key"]) == "on"
            # Persist if it carries any config, or to remember a non-default (hidden) state.
            if label or lead or incoming or not enabled:
                entry = {"label": label, "lead_time": lead, "enabled": enabled}
                if incoming:
                    entry["incoming_label"] = incoming
                settings[s["key"]] = entry
        storage.save_warehouse_settings(settings)
        return redirect(url_for("admin_warehouses"))

    saved = storage.load_warehouse_settings()
    for s in sources:
        cfg = saved.get(s["key"], {})
        s["label"] = cfg.get("label", "")
        s["lead_time"] = storage.lead_time_value(cfg)
        s["incoming_label"] = cfg.get("incoming_label", "")
        s["enabled"] = cfg.get("enabled", True)   # default: selectable
    return render_template(
        "warehouses.html",
        username=current_user.id, sidebar_active="warehouses",
        sources=sources, odoo_ok=odoo_ok, odoo_msg=odoo_msg,
    )


@app.route("/inventory/categories", methods=["GET", "POST"])
@admin_required
def admin_categories():
    odoo_ok, odoo_msg = True, ""
    categories = []
    try:
        uid, models = odoo_client.connect()
        categories = odoo_client.list_categories(models, uid)
    except Exception:
        odoo_ok, odoo_msg = False, "Odoo unavailable — category list could not load."

    if request.method == "POST":
        by_id = {c["id"]: c["complete_name"] for c in categories}
        chosen = []
        for cid in request.form.getlist("available"):
            try:
                cid = int(cid)
            except ValueError:
                continue
            chosen.append({"id": cid, "complete_name": by_id.get(cid, str(cid))})
        storage.save_available_categories(chosen)
        return redirect(url_for("admin_categories"))

    selected_ids = {c["id"] for c in storage.load_available_categories()}
    return render_template(
        "categories.html",
        username=current_user.id, sidebar_active="categories",
        categories=categories, selected_ids=selected_ids,
        odoo_ok=odoo_ok, odoo_msg=odoo_msg,
    )


@app.route("/inventory/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        storage.save_settings(request.form)
        return redirect(url_for("admin_settings"))
    return render_template(
        "settings.html",
        username=current_user.id, sidebar_active="settings",
        settings=storage.load_settings(),
    )


# Downloadable documentation (admin only) — generated live from current config.
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOC_BUILDERS = {
    "customer-guide.docx": ("MDR Inventory API - Customer Guide.docx", docgen.build_customer_guide),
    "internal-reference.docx": ("MDR Inventory API - Internal Reference Guide.docx", docgen.build_internal_reference),
}


@app.route("/inventory/docs-download/<name>")
@admin_required
def download_doc(name):
    entry = DOC_BUILDERS.get(name)
    if not entry:
        abort(404)
    download_name, builder = entry
    buf = builder()
    return send_file(buf, as_attachment=True, download_name=download_name, mimetype=DOCX_MIME)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5004, threaded=True)
