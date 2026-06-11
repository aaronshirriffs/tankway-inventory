"""
Export column registry — the single source of truth for column inclusion,
order, and labelling in emailed XLSX/CSV exports.

Every value is derived from the same build_products() item that /v1/products
returns (plus read-only extras fetched separately for export-only columns), so
an export can never disagree with the live API about filtering, stock,
pricing, or incoming logic. The live API response itself is built directly in
build_products() and is NOT routed through this module — its output stays
byte-identical to before this feature existed.
"""
GST_RATE = 0.15  # NZ GST; "GST inc" prices are exclusive price x 1.15


def _round2(v):
    return round(v, 2) if isinstance(v, (int, float)) else v


def _num(v):
    """Render whole floats as ints so cells read '25' not '25.0'."""
    if isinstance(v, float) and v == int(v):
        return int(v)
    return v


def _mapping_label(config, index):
    mappings = config.get("warehouse_mappings") or []
    return mappings[index]["label"] if len(mappings) > index else None


def _stock(item, config, extras):
    """Compulsory stock column = the key's FIRST warehouse mapping (the
    'Available Immediately' pool, e.g. Tiki Place + Cessna Road combined)."""
    label = _mapping_label(config, 0)
    if label is None:
        return ""
    return _num(item.get("stock", {}).get(label, 0.0))


def _secondary(item, config, extras):
    """Second warehouse mapping as 'qty (lead time)'. Blank if the key has no
    second mapping. A third+ mapping is not rendered (build later if needed)."""
    label = _mapping_label(config, 1)
    if label is None:
        return ""
    for a in item.get("availability", []):
        if a.get("label") == label:
            qty = _num(a.get("qty", 0.0))
            lead = a.get("lead_time") or ""
            return f"{qty} ({lead})" if lead else f"{qty}"
    return ""


def _incoming(item, config, extras):
    """Same data the API returns in incoming[]: total outstanding confirmed-PO
    quantity, with the earliest expected date, e.g. '20 (due 2026-06-15)'."""
    shipments = item.get("incoming") or []
    if not shipments:
        return ""
    total = _num(round(sum(s.get("quantity") or 0.0 for s in shipments), 2))
    dates = [s.get("expected_date") for s in shipments if s.get("expected_date")]
    return f"{total} (due {min(dates)})" if dates else f"{total}"


def _price_exc(item, config, extras):
    v = item.get("sales_price")
    return _num(_round2(v)) if v is not None else ""


def _price_inc(item, config, extras):
    v = item.get("sales_price")
    return _num(_round2(v * (1 + GST_RATE))) if v is not None else ""


def _buy_price(item, config, extras):
    # your_price exists only when the key has a pricelist assigned.
    v = item.get("your_price")
    return _num(_round2(v)) if v is not None else ""


def _weight(item, config, extras):
    return _num((extras.get(item["id"]) or {}).get("weight", ""))


def _name(item, config, extras):
    """Product name, with the variant attribute suffix appended in the export so
    different sizes/colours/etc of the same template are distinguishable
    (e.g. 'Utorm mobile system package (2x Frames high)'). The suffix comes from
    fetch_export_extras() — the live API path is unchanged."""
    base = item.get("name") or ""
    suffix = (extras.get(item["id"]) or {}).get("variant_suffix", "")
    return base + suffix


def _extra(field):
    def get(item, config, extras):
        return (extras.get(item["id"]) or {}).get(field, "")
    return get


# Fixed left-to-right order. toggle=None -> compulsory (always exported);
# otherwise the column is included only when export.columns[toggle] is true on
# the key — the same per-customer opt-in pattern as show_price / show_incoming.
COLUMNS = [
    {"toggle": "inv_category",  "label": "Internal Category",                "get": _extra("inv_category")},
    {"toggle": None,            "label": "Sku",                              "get": lambda i, c, e: i.get("sku") or ""},
    {"toggle": None,            "label": "Name",                             "get": _name},
    {"toggle": None,            "label": "Stock (Available Immediately)",    "get": _stock},
    {"toggle": "secondary",     "label": "Secondary Warehouse Availability", "get": _secondary},
    {"toggle": "incoming",      "label": "Incoming Stock Available",         "get": _incoming},
    {"toggle": "price_exc",     "label": "RRP (GST exc)",                    "get": _price_exc},
    {"toggle": "price_inc",     "label": "RRP (GST inc)",                    "get": _price_inc},
    # Label is deliberately generic — never a customer or pricelist name.
    {"toggle": "buy_price",     "label": "Your Buy Price (GST exc)",         "get": _buy_price},
    {"toggle": "weight",        "label": "Volumetric Weight",                "get": _weight},
    {"toggle": "ecom_category", "label": "B2B portal category/s",            "get": _extra("ecom_category")},
]

COLUMN_TOGGLES = [c["toggle"] for c in COLUMNS if c["toggle"]]

# Toggles whose data comes from fetch_export_extras() rather than build_products().
EXTRA_TOGGLES = {"weight", "inv_category", "ecom_category"}


def active_columns(column_settings):
    """Columns to export, in fixed order, for a key's export.columns dict."""
    on = column_settings or {}
    return [c for c in COLUMNS if c["toggle"] is None or on.get(c["toggle"])]


def build_rows(products, config, extras, column_settings):
    """-> (headers, rows) for the export file."""
    cols = active_columns(column_settings)
    headers = [c["label"] for c in cols]
    rows = [[c["get"](item, config, extras) for c in cols] for item in products]
    return headers, rows
