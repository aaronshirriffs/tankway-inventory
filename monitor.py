"""
Correctness monitor for the MDR Inventory API.

Runs invariant checks against exactly what each customer receives
(build_products output) and independently cross-checks incoming dates against
the receipts in Odoo. Anomalies show on the admin Status page; an email goes to
ALERT_EMAIL only when the anomaly set is NEW or CHANGED, so a persistent known
issue doesn't email on every run.

The incoming cross-check is deliberately decoupled from get_incoming_stock: it
recomputes the "correct" receipt scheduled date here, so if that function ever
regresses (e.g. reverts to the PO date), the mismatch is caught.
"""
import json
import logging
import os

import gmail_client
import odoo_client
import storage

logger = logging.getLogger(__name__)

ALERT_EMAIL = "aaron@mdrlighting.co.nz"
STATE_FILE = os.path.join(os.path.dirname(__file__), "monitor_state.json")
_MAX_DETAIL = 25  # cap examples per key in the alert email / state

RULE_LABELS = {
    "empty_feed": "Feed returned 0 products",
    "negative_stock": "Negative stock quantity",
    "zero_sales_price": "Zero / missing sales price",
    "buy_over_sale": "Buy price exceeds sales price",
    "compare_below_sale": "Compare price below sales price",
    "duplicate_name_sku": "Duplicate product (name + SKU)",
    "incoming_date_mismatch": "Incoming date != receipt scheduled date",
    "check_error": "Monitor check errored",
}


def _truth_incoming_earliest(models, uid, product_ids):
    """Independent 'correct' earliest incoming date per product: the min
    scheduled date of open incoming PURCHASE receipts. Computed here rather than
    via get_incoming_stock so a regression there is caught by comparison."""
    _, db, _, key = odoo_client._creds()
    out = {}
    if not product_ids:
        return out
    moves = models.execute_kw(
        db, uid, key, "stock.move", "search_read",
        [[["product_id", "in", list(product_ids)],
          ["picking_type_id.code", "=", "incoming"],
          ["purchase_line_id", "!=", False],
          ["state", "in", ["assigned", "confirmed", "waiting", "partially_available"]]]],
        {"fields": ["product_id", "product_uom_qty", "quantity_done", "date"]},
    )
    for m in moves:
        rem = (m.get("product_uom_qty") or 0.0) - (m.get("quantity_done") or 0.0)
        if rem <= 0 or not m.get("product_id"):
            continue
        pid = m["product_id"][0]
        d = str(m.get("date") or "")[:10]
        if d and (out.get(pid) is None or d < out[pid]):
            out[pid] = d
    return out


def _check_key(config):
    """Return (violations, product_count) for one key's live output."""
    from app import build_products  # late import; app fully loaded at call time
    violations = []

    def add(rule, p, detail):
        violations.append({"rule": rule, "product_id": (p or {}).get("id"),
                           "name": (p or {}).get("name"), "detail": detail})

    # clamp_stock=False so the monitor still sees raw negative stock (the API
    # itself clamps those to 0 for customers).
    products = build_products(config, clamp_stock=False)
    if not products:
        add("empty_feed", None, "key returns 0 products")
        return violations, 0

    seen = {}
    for p in products:
        for lbl, q in (p.get("stock") or {}).items():
            if isinstance(q, (int, float)) and q < 0:
                add("negative_stock", p, f"{lbl} = {q}")
        sp, yp, cp = p.get("sales_price"), p.get("your_price"), p.get("compare_price")
        if "sales_price" in p and (sp is None or sp == 0):
            add("zero_sales_price", p, "sales_price is 0/none")
        if sp not in (None, "") and yp not in (None, "") and yp > sp:
            add("buy_over_sale", p, f"buy {yp} > sales {sp}")
        if p.get("has_compare_price") and cp not in (None, "") and sp not in (None, "") and cp < sp:
            add("compare_below_sale", p, f"compare {cp} < sales {sp}")
        skey = (p.get("name"), p.get("sku"))
        if skey in seen:
            add("duplicate_name_sku", p, f"same name+SKU as product {seen[skey]}")
        else:
            seen[skey] = p.get("id")

    incoming_products = [p for p in products if p.get("incoming")]
    if incoming_products:
        uid, models = odoo_client.connect()
        truth = _truth_incoming_earliest(models, uid, [p["id"] for p in incoming_products])
        for p in incoming_products:
            dates = [s.get("expected_date") for s in p["incoming"] if s.get("expected_date")]
            api_earliest = min(dates) if dates else None
            t = truth.get(p["id"])
            if t and api_earliest and t != api_earliest:
                add("incoming_date_mismatch", p, f"API {api_earliest} != receipt scheduled {t}")
    return violations, len(products)


def run_checks():
    """Run all enabled keys. Returns a structured report (never raises per key)."""
    results = []
    for token, config in storage.load_keys().items():
        if not config.get("enabled"):
            continue
        try:
            violations, count = _check_key(config)
        except Exception as e:
            violations = [{"rule": "check_error", "product_id": None, "name": None,
                           "detail": f"check failed: {e}"}]
            count = None
        results.append({"label": config.get("label"), "token": token,
                        "count": count, "violations": violations})
    total = sum(len(r["violations"]) for r in results)
    return {"checked_at": storage.now_iso(), "results": results,
            "total_violations": total, "ok": total == 0}


def _violation_keys(report):
    """Coarse identity per anomaly (customer|rule|product) — deliberately WITHOUT
    the exact detail, so a negative stock going -1 -> -2 isn't treated as a new
    anomaly. Used to email only when a genuinely new anomaly appears."""
    return {f"{r['label']}|{v['rule']}|{v['product_id']}"
            for r in report["results"] for v in r["violations"]}


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def latest_report():
    """The last stored report (for the Status page). None if never run."""
    return load_state().get("last_report")


def run_now():
    """Run checks on demand (admin button): refresh the stored report but send
    NO email and don't touch the alert baseline. Returns the report."""
    report = run_checks()
    state = load_state()
    state["last_report"] = report
    _save_state(state)
    return report


def _alert_body(report):
    lines = ["The MDR Inventory correctness monitor found anomalies in what one or",
             "more customers currently receive.", "",
             f"Checked at {report['checked_at']} UTC.", ""]
    for r in report["results"]:
        if not r["violations"]:
            continue
        lines.append(f"== {r['label']} — {len(r['violations'])} issue(s) ==")
        for v in r["violations"][:_MAX_DETAIL]:
            who = f"[{v['product_id']}] {v['name']}" if v.get("product_id") else "(feed)"
            lines.append(f"  - {RULE_LABELS.get(v['rule'], v['rule'])}: {who} — {v['detail']}")
        extra = len(r["violations"]) - _MAX_DETAIL
        if extra > 0:
            lines.append(f"  ... and {extra} more")
        lines.append("")
    lines.append("Full detail: https://tools.tankway.co.nz/inventory/status")
    return "\n".join(lines)


def run_and_alert():
    """Scheduler entry point. Runs checks, persists the report, and emails
    ALERT_EMAIL only when a NEW anomaly appears (one not in the last alerted
    set) — so persistent known issues don't re-email daily and a clearing issue
    is silent. Never raises."""
    try:
        report = run_checks()
    except Exception:
        logger.exception("monitor: run_checks failed")
        return None
    state = load_state()
    current = _violation_keys(report)
    alerted = set(state.get("alerted_keys", []))
    new = current - alerted
    try:
        if new and gmail_client.is_authorised():
            gmail_client.send_plain(
                to=ALERT_EMAIL,
                subject=f"MDR Inventory: {len(new)} new data anomaly(ies) detected",
                body=_alert_body(report),
            )
            logger.info("monitor: alert emailed (%d new, %d total)",
                        len(new), report["total_violations"])
    except Exception:
        logger.exception("monitor: alert send failed")
    # Baseline becomes whatever is currently wrong: new issues won't re-alert,
    # and a cleared issue that recurs later counts as new again.
    state["alerted_keys"] = sorted(current)
    state["last_report"] = report
    _save_state(state)
    return report
