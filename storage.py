"""
Persistence for API keys + lightweight runtime state.

keys.json is the source of truth for key configuration, following the same
on-disk-JSON pattern as the platform's users.json. Writes are atomic
(temp file + os.replace) and guarded by a lock so concurrent admin edits and
API requests can't corrupt the file.

The per-key activity log is a bounded ring buffer per key, persisted to
activity.json (atomic writes, same as keys.json) so it survives service restarts.
"""
import json
import os
import re
import secrets
import shutil
import threading
from collections import deque
from datetime import datetime, timezone

KEYS_FILE = os.path.join(os.path.dirname(__file__), "keys.json")

# Lead time is free text (e.g. "5-7" or "next week"). A purely numeric value
# (or numeric range) gets " days" appended for display.
_NUMERIC_LEAD = re.compile(r"^[\d\s\-–—/]+$")


def lead_time_value(entry):
    """Raw lead-time string as stored. Legacy integer lead_time_days -> str."""
    if not isinstance(entry, dict):
        return str(entry or "").strip()
    if entry.get("lead_time") not in (None, ""):
        return str(entry["lead_time"]).strip()
    v = entry.get("lead_time_days")
    if v in (None, "", 0, "0"):
        return ""
    return str(v).strip()


def lead_time_display(value):
    """Human-readable lead time, e.g. '5-7 days'. Accepts an entry dict or raw string. '' when none."""
    raw = lead_time_value(value)
    if not raw:
        return ""
    return raw + " days" if _NUMERIC_LEAD.match(raw) else raw


def lead_time_rank(value):
    """Sortable magnitude for a lead time (largest number found), -1 if none."""
    raw = lead_time_value(value)
    nums = re.findall(r"\d+", raw)
    return max(int(n) for n in nums) if nums else -1

_lock = threading.RLock()
_activity = {}          # token -> deque[{timestamp, ip, product_count, rate_limited}]
_ACTIVITY_MAX = 200     # entries retained per key
ACTIVITY_FILE = os.path.join(os.path.dirname(__file__), "activity.json")


def _save_activity():
    """Atomically persist the activity ring buffers. Call while holding _lock."""
    data = {tok: list(buf) for tok, buf in _activity.items()}
    tmp = ACTIVITY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, ACTIVITY_FILE)


def _load_activity():
    """Load persisted activity into the in-memory ring buffers at startup."""
    try:
        with open(ACTIVITY_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return
    with _lock:
        for tok, entries in data.items():
            _activity[tok] = deque(entries, maxlen=_ACTIVITY_MAX)


_load_activity()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------
# keys.json load / save
# ----------------------------------------------------------------------------
def load_keys():
    """Return the keys dict {token: config}. Missing/empty file -> {}."""
    with _lock:
        if not os.path.exists(KEYS_FILE):
            return {}
        try:
            with open(KEYS_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}


def save_keys(keys):
    """Atomically persist the keys dict.

    Before overwriting, the current file is copied to keys.json.bak, so the
    immediately-previous version is always one step away if a save writes bad
    data. (Daily off-app backups are handled separately by the system backup
    job, which also archives this file.)"""
    with _lock:
        if os.path.exists(KEYS_FILE):
            try:
                shutil.copy2(KEYS_FILE, KEYS_FILE + ".bak")
            except OSError:
                pass  # a backup failure must never block a legitimate save
        tmp = KEYS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(keys, f, indent=2)
        os.replace(tmp, KEYS_FILE)


def get_key(token):
    return load_keys().get(token)


# ----------------------------------------------------------------------------
# Global admin config (warehouses / categories / settings)
# Each is a small JSON file alongside keys.json, written atomically.
# ----------------------------------------------------------------------------
_DIR = os.path.dirname(__file__)
WAREHOUSE_SETTINGS_FILE = os.path.join(_DIR, "warehouse_settings.json")  # {"loc:49": {"label": "...", "lead_time_days": 10}, ...}
CATEGORY_SETTINGS_FILE = os.path.join(_DIR, "category_settings.json")    # {"available": [{"id": 1, "complete_name": "..."}]}
SETTINGS_FILE = os.path.join(_DIR, "settings.json")                      # {"default_rate_limit_per_minute": 5, ...}

DEFAULT_SETTINGS = {
    "default_rate_limit_per_minute": 5,
    "default_rate_limit_daily": 200,
    "default_burst_allowance": 3,
}


def _load_json(path, default):
    with _lock:
        if not os.path.exists(path):
            return json.loads(json.dumps(default))
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError, OSError):
            return json.loads(json.dumps(default))


def _save_json(path, data):
    with _lock:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)


def load_warehouse_settings():
    """{ 'wh:1'|'loc:49': {label, lead_time_days} }."""
    d = _load_json(WAREHOUSE_SETTINGS_FILE, {})
    return d if isinstance(d, dict) else {}


def save_warehouse_settings(d):
    _save_json(WAREHOUSE_SETTINGS_FILE, d or {})


def load_available_categories():
    """List of {id, complete_name} curated for use in key generation. [] = all allowed."""
    d = _load_json(CATEGORY_SETTINGS_FILE, {"available": []})
    return d.get("available", []) if isinstance(d, dict) else []


def save_available_categories(lst):
    _save_json(CATEGORY_SETTINGS_FILE, {"available": lst or []})


def load_settings():
    """Global settings merged over defaults."""
    s = dict(DEFAULT_SETTINGS)
    d = _load_json(SETTINGS_FILE, {})
    if isinstance(d, dict):
        s.update({k: v for k, v in d.items() if k in DEFAULT_SETTINGS})
    return s


def save_settings(d):
    cur = load_settings()
    for k in DEFAULT_SETTINGS:
        if k in d:
            try:
                cur[k] = int(d[k])
            except (TypeError, ValueError):
                pass
    _save_json(SETTINGS_FILE, cur)


# ----------------------------------------------------------------------------
# Key lifecycle
# ----------------------------------------------------------------------------
def new_key(label, allowed_categories=None, excluded_skus=None,
            warehouse_mappings=None, show_price=False, expiry=None,
            rate_limit_per_minute=5, rate_limit_daily=200, burst_allowance=3,
            excluded_categories=None, show_incoming=False, pricelist=None,
            export=None, show_compare=False):
    """Create and persist a new key. Returns (token, config)."""
    token = "mdr_" + secrets.token_urlsafe(32)
    config = {
        "token": token,
        "label": label,
        "enabled": True,
        "created_at": now_iso(),
        "last_used": None,
        "expiry": expiry or None,
        "allowed_categories": allowed_categories or [],
        "excluded_categories": excluded_categories or [],
        "excluded_skus": excluded_skus or [],
        "warehouse_mappings": warehouse_mappings or [],
        "pricelist": pricelist or None,
        "show_price": bool(show_price),
        "show_incoming": bool(show_incoming),
        "show_compare": bool(show_compare),
        "rate_limit_per_minute": int(rate_limit_per_minute),
        "rate_limit_daily": int(rate_limit_daily),
        "burst_allowance": int(burst_allowance),
        # Email-export settings (None = feature off; see exporter.DEFAULT_EXPORT).
        "export": export,
    }
    with _lock:
        keys = load_keys()
        keys[token] = config
        save_keys(keys)
    return token, config


def update_key(token, **fields):
    """Patch fields on an existing key. Returns the updated config or None."""
    with _lock:
        keys = load_keys()
        if token not in keys:
            return None
        keys[token].update(fields)
        save_keys(keys)
        return keys[token]


def delete_key(token):
    with _lock:
        keys = load_keys()
        if token in keys:
            del keys[token]
            save_keys(keys)
            _activity.pop(token, None)
            _save_activity()
            return True
        return False


def touch_last_used(token):
    """Record that a key was just used (persisted)."""
    update_key(token, last_used=now_iso())


# ----------------------------------------------------------------------------
# Activity log (in-memory ring buffer per key)
# ----------------------------------------------------------------------------
def log_activity(token, ip, product_count, rate_limited):
    with _lock:
        buf = _activity.setdefault(token, deque(maxlen=_ACTIVITY_MAX))
        buf.appendleft({
            "timestamp": now_iso(),
            "ip": ip,
            "product_count": product_count,
            "rate_limited": rate_limited,
        })
        _save_activity()


def get_activity(token):
    with _lock:
        return list(_activity.get(token, []))
