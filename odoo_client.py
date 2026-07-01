"""
Odoo 16 XML-RPC client for the MDR Inventory API.

Authentication uses the dedicated system@ service account with an Odoo
API key (passed in place of the password — Odoo accepts an API key in the
XML-RPC `authenticate` call). All credentials come from environment
variables loaded from .env. Nothing here is ever exposed to the public API
surface.
"""
import os
import xmlrpc.client
from datetime import datetime, timezone


def _creds():
    return (
        os.environ["ODOO_URL"].rstrip("/"),
        os.environ["ODOO_DB"],
        os.environ["ODOO_USERNAME"],
        os.environ["ODOO_API_KEY"],
    )


def connect():
    """Authenticate and return (uid, models_proxy). Raises on failure."""
    url, db, user, key = _creds()
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, user, key, {})
    if not uid:
        raise ConnectionError("Odoo authentication failed.")
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
    return uid, models


def test_connection():
    """Returns (True, message) or (False, error_message)."""
    try:
        uid, _ = connect()
        return True, f"Connected (UID {uid})"
    except Exception as e:
        return False, str(e)


def list_warehouses(models, uid):
    """All Odoo warehouses: [{id, name, code}]. Used to build per-key mappings."""
    _, db, _, key = _creds()
    return models.execute_kw(
        db, uid, key,
        "stock.warehouse", "search_read",
        [[]],
        {"fields": ["id", "name", "code"], "order": "name"},
    )


def list_locations(models, uid):
    """
    External / partner stock locations under EXT (e.g. EXT/Stock/AUS/Showtools),
    as [{id, complete_name}]. These are internal stock.location records that can
    be mapped individually per key, separately from the whole-warehouse mappings.
    """
    _, db, _, key = _creds()
    return models.execute_kw(
        db, uid, key,
        "stock.location", "search_read",
        [[["usage", "=", "internal"], ["complete_name", "=like", "EXT/%"]]],
        {"fields": ["id", "complete_name"], "order": "complete_name"},
    )


def list_categories(models, uid):
    """All product categories: [{id, complete_name}]. Used to build per-key visibility."""
    _, db, _, key = _creds()
    return models.execute_kw(
        db, uid, key,
        "product.category", "search_read",
        [[]],
        {"fields": ["id", "complete_name"], "order": "complete_name"},
    )


def _kit_template_ids(models, uid):
    """product.template ids that are kit packages (have a phantom/'Kit' BoM).

    Kits are typically Consumable-type products, so they'd be missed by the
    storable-only filter; we include them explicitly. Returns [] if MRP isn't
    reachable, so the feed degrades gracefully to storable-only.
    """
    _, db, _, key = _creds()
    try:
        boms = models.execute_kw(
            db, uid, key,
            "mrp.bom", "search_read",
            [[["type", "=", "phantom"]]],
            {"fields": ["product_tmpl_id"]},
        )
    except Exception:
        return []
    return list({b["product_tmpl_id"][0] for b in boms if b.get("product_tmpl_id")})


def fetch_products(models, uid, category_ids, excluded_skus, excluded_category_ids=None):
    """
    Storable products AND kit packages within the given categories (and their
    sub-categories), minus any excluded sub-category subtrees and any excluded
    SKUs. Only products published on the Odoo website (is_published = True) are
    returned.

    Kits are products with a phantom BoM; Odoo reports their qty_available /
    free_qty as the number buildable from current component stock, so they flow
    through the normal per-warehouse stock path with no special handling.

    Returns a list of raw Odoo dicts:
        {id, name, default_code, list_price, write_date}
    """
    _, db, _, key = _creds()

    # Only surface products that are published on the company's Odoo website.
    domain = [["is_published", "=", True]]
    # Include storable products OR kit packages (phantom BoM, usually 'consu').
    kit_tmpl_ids = _kit_template_ids(models, uid)
    if kit_tmpl_ids:
        domain += ["|", ["type", "=", "product"], ["product_tmpl_id", "in", kit_tmpl_ids]]
    else:
        domain.append(["type", "=", "product"])
    if category_ids:
        domain.append(["categ_id", "child_of", list(category_ids)])
    else:
        # No categories assigned to this key => no products visible.
        return []
    if excluded_category_ids:
        # Exclude these categories and all of their sub-categories. The leading
        # "!" negates the single child_of leaf that follows it (Odoo domain).
        domain += ["!", ["categ_id", "child_of", list(excluded_category_ids)]]
    if excluded_skus:
        domain.append(["default_code", "not in", list(excluded_skus)])

    products = models.execute_kw(
        db, uid, key,
        "product.product", "search_read",
        [domain],
        # lst_price (not list_price) is the per-variant sales price: it includes
        # the variant's price_extra, so different sizes/colours of one template
        # carry their correct prices. For non-variant products lst_price == list_price.
        {"fields": ["id", "name", "default_code", "lst_price", "write_date",
                    "compare_list_price", "product_template_attribute_value_ids"],
         "order": "name"},
    )
    _append_variant_suffix(models, uid, products)
    return products


def _append_variant_suffix(models, uid, products):
    """Append each product's variant attribute values to its name in-place, e.g.
    'Utorm mobile system package' -> 'Utorm mobile system package (2x Frames high)',
    so variants that share a template name are distinguishable. No-op for
    non-variant products. Used in the shared product path so the API and the
    email export name variants identically."""
    _, db, _, key = _creds()
    av_ids = sorted({i for p in products
                     for i in (p.get("product_template_attribute_value_ids") or [])})
    if not av_ids:
        return
    av_name = {}
    try:
        for a in models.execute_kw(db, uid, key, "product.template.attribute.value",
                                   "read", [av_ids], {"fields": ["name"]}):
            av_name[a["id"]] = a.get("name")
    except Exception:
        return
    for p in products:
        vals = [av_name.get(i) for i in (p.get("product_template_attribute_value_ids") or [])
                if av_name.get(i)]
        if vals:
            p["name"] = (p.get("name") or "") + " (" + ", ".join(vals) + ")"


def stock_by_warehouse(models, uid, product_ids, warehouse_ids):
    """
    Available (free-to-use) quantity per (warehouse, product).

    Returns { warehouse_id: { product_id: free_qty } }.

    Uses Odoo's context-scoped `free_qty` (= on hand - reserved): reading
    product.product with context {'warehouse': wh_id} returns the quantity
    available to promise in that warehouse's stock locations only, excluding
    units already reserved against confirmed sales orders.
    """
    _, db, _, key = _creds()
    result = {}
    if not product_ids or not warehouse_ids:
        return result

    ids = list(product_ids)
    for wh_id in warehouse_ids:
        recs = models.execute_kw(
            db, uid, key,
            "product.product", "read",
            [ids],
            {"fields": ["free_qty"], "context": {"warehouse": wh_id}},
        )
        result[wh_id] = {r["id"]: r.get("free_qty", 0.0) for r in recs}
    return result


def stock_by_location(models, uid, product_ids, location_ids):
    """
    Available (free-to-use) quantity per (stock location, product).

    Returns { location_id: { product_id: free_qty } }.

    Uses Odoo's context-scoped `free_qty` (= on hand - reserved) with context
    {'location': loc_id}, which returns the available-to-promise quantity for
    that location and its children only (excluding units reserved against
    confirmed sales orders). Used for mapping individual external/partner
    locations (e.g. EXT/Stock/AUS/Showtools).
    """
    _, db, _, key = _creds()
    result = {}
    if not product_ids or not location_ids:
        return result

    ids = list(product_ids)
    for loc_id in location_ids:
        recs = models.execute_kw(
            db, uid, key,
            "product.product", "read",
            [ids],
            {"fields": ["free_qty"], "context": {"location": loc_id}},
        )
        result[loc_id] = {r["id"]: r.get("free_qty", 0.0) for r in recs}
    return result


def get_incoming_stock(models, uid, product_ids):
    """
    Confirmed incoming stock per product, derived from purchase order lines.

    Includes lines whose purchase order is in state 'purchase' or 'done' and that
    are not yet fully received (ordered qty > received qty — i.e. an incoming
    picking still outstanding). The destination warehouse is resolved via the
    order's picking type so the caller can map it to a customer-facing label.

    Returns:
        { product_id: [ {quantity, expected_date, warehouse_id}, ... ] }
        - quantity:      ordered quantity not yet received
        - expected_date: date_planned as "YYYY-MM-DD" (or None)
        - warehouse_id:  destination warehouse id (or None if undetermined)
    """
    _, db, _, key = _creds()
    result = {}
    if not product_ids:
        return result

    lines = models.execute_kw(
        db, uid, key,
        "purchase.order.line", "search_read",
        [[["product_id", "in", list(product_ids)],
          ["order_id.state", "in", ["purchase", "done"]]]],
        {"fields": ["product_id", "product_qty", "qty_received", "date_planned", "order_id"]},
    )
    if not lines:
        return result

    # Resolve each PO's destination warehouse via picking_type_id.warehouse_id.
    order_ids = list({l["order_id"][0] for l in lines if l.get("order_id")})
    po_wh = {}
    try:
        orders = models.execute_kw(
            db, uid, key,
            "purchase.order", "read",
            [order_ids], {"fields": ["picking_type_id"]},
        )
        ptype_ids = list({o["picking_type_id"][0] for o in orders if o.get("picking_type_id")})
        ptypes = models.execute_kw(
            db, uid, key,
            "stock.picking.type", "read",
            [ptype_ids], {"fields": ["warehouse_id"]},
        ) if ptype_ids else []
        ptype_wh = {p["id"]: (p["warehouse_id"][0] if p.get("warehouse_id") else None) for p in ptypes}
        for o in orders:
            pt = o.get("picking_type_id")
            po_wh[o["id"]] = ptype_wh.get(pt[0]) if pt else None
    except Exception:
        po_wh = {}

    for l in lines:
        remaining = (l.get("product_qty") or 0.0) - (l.get("qty_received") or 0.0)
        if remaining <= 0:
            continue
        if not l.get("product_id"):
            continue
        pid = l["product_id"][0]
        dp = l.get("date_planned") or ""
        oid = l["order_id"][0] if l.get("order_id") else None
        result.setdefault(pid, []).append({
            "quantity": round(remaining, 2),
            "expected_date": (str(dp)[:10] if dp else None),
            "warehouse_id": po_wh.get(oid),
        })

    for pid in result:
        result[pid].sort(key=lambda s: s.get("expected_date") or "9999-99-99")
    return result


def fetch_export_extras(models, uid, product_ids):
    """
    Export-only product fields: weight, inventory category, eCommerce categories.
    Used solely by the email-export engine — the /v1/products path never calls
    this, so the live API's Odoo footprint is unchanged. (Variant naming is
    handled in the shared product path, not here.)

    Returns { product_id: {weight, inv_category, ecom_category} } where
    ecom_category is the website categories as 'Parent > Name' comma-joined.
    """
    _, db, _, key = _creds()
    out = {}
    if not product_ids:
        return out

    recs = models.execute_kw(
        db, uid, key,
        "product.product", "read",
        [list(product_ids)],
        {"fields": ["weight", "categ_id", "public_categ_ids"]},
    )

    pc_ids = sorted({i for r in recs for i in (r.get("public_categ_ids") or [])})
    pc_name = {}
    if pc_ids:
        try:
            cats = models.execute_kw(
                db, uid, key,
                "product.public.category", "read",
                [pc_ids], {"fields": ["name", "parent_id"]},
            )
            for c in cats:
                parent = c.get("parent_id")
                pc_name[c["id"]] = f"{parent[1]} > {c['name']}" if parent else c["name"]
        except Exception:
            pc_name = {}

    for r in recs:
        out[r["id"]] = {
            "weight": r.get("weight") or 0.0,
            "inv_category": r["categ_id"][1] if r.get("categ_id") else "",
            "ecom_category": ", ".join(
                pc_name.get(i, "") for i in (r.get("public_categ_ids") or []) if pc_name.get(i)
            ),
        }
    return out


def list_pricelists(models, uid):
    """All sale pricelists: [{id, name}]. Used to assign a customer buy-price list per key."""
    _, db, _, key = _creds()
    return models.execute_kw(
        db, uid, key,
        "product.pricelist", "search_read",
        [[]],
        {"fields": ["id", "name"], "order": "name"},
    )


def pricelist_prices(models, uid, pricelist_id, product_ids, qty=1):
    """
    Customer buy price per product under a pricelist, computed from its rules.

    Replicates Odoo's rule resolution for the rule shapes used here:
      - applied_on: global / product category (incl. sub-categories) / template / variant
      - compute_price: percentage or fixed off the base, or a simple formula
                       (discount % + surcharge)
      - base: list_price or standard_price  ('pricelist' recursion -> falls back to list_price)
    Rules are evaluated in Odoo's order (applied_on, min_quantity desc, categ_id desc,
    id desc); the first applicable rule for the given quantity wins. No rule -> list_price.

    Returns { product_id: unit_price }.
    """
    _, db, _, key = _creds()
    result = {}
    if not pricelist_id or not product_ids:
        return result
    ids = list(product_ids)

    prods = models.execute_kw(
        db, uid, key,
        "product.product", "read",
        [ids],
        # lst_price = per-variant sales price (base + price_extra); the pricelist
        # "Sales Price" base must use this so variant buy prices are correct.
        {"fields": ["lst_price", "standard_price", "categ_id", "product_tmpl_id"]},
    )

    categ_ids = list({p["categ_id"][0] for p in prods if p.get("categ_id")})
    cats = models.execute_kw(
        db, uid, key,
        "product.category", "read",
        [categ_ids], {"fields": ["parent_path"]},
    ) if categ_ids else []
    ancestors = {
        c["id"]: {int(x) for x in (c.get("parent_path") or "").strip("/").split("/") if x}
        for c in cats
    }

    items = models.execute_kw(
        db, uid, key,
        "product.pricelist.item", "search_read",
        [[["pricelist_id", "=", pricelist_id]]],
        {"fields": ["applied_on", "categ_id", "product_tmpl_id", "product_id",
                    "compute_price", "fixed_price", "percent_price", "price_discount",
                    "price_surcharge", "base", "min_quantity", "date_start", "date_end"],
         "order": "applied_on, min_quantity desc, categ_id desc, id desc"},
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _date_ok(it):
        ds, de = it.get("date_start"), it.get("date_end")
        if ds and str(ds) > now:
            return False
        if de and str(de) < now:
            return False
        return True

    items = [it for it in items if _date_ok(it)]

    for p in prods:
        list_price = p.get("lst_price") or 0.0   # per-variant sales price (incl. price_extra)
        cost = p.get("standard_price") or 0.0
        pcat = p["categ_id"][0] if p.get("categ_id") else None
        ancs = ancestors.get(pcat, set())
        tmpl = p["product_tmpl_id"][0] if p.get("product_tmpl_id") else None

        chosen = None
        for it in items:
            if qty < (it.get("min_quantity") or 0):
                continue
            ao = it["applied_on"]
            if ao == "3_global":
                ok = True
            elif ao == "2_product_category":
                cid = it["categ_id"][0] if it.get("categ_id") else None
                ok = cid in ancs
            elif ao == "1_product":
                tid = it["product_tmpl_id"][0] if it.get("product_tmpl_id") else None
                ok = tid == tmpl
            elif ao == "0_product_variant":
                vid = it["product_id"][0] if it.get("product_id") else None
                ok = vid == p["id"]
            else:
                ok = False
            if ok:
                chosen = it
                break

        if not chosen:
            result[p["id"]] = round(list_price, 2)
            continue

        base = {"list_price": list_price, "standard_price": cost}.get(chosen.get("base"), list_price)
        compute = chosen.get("compute_price")
        if compute == "fixed":
            price = chosen.get("fixed_price") or 0.0
        elif compute == "percentage":
            price = base * (1 - (chosen.get("percent_price") or 0.0) / 100.0)
        else:  # 'formula' (basic): discount % then surcharge
            price = base * (1 - (chosen.get("price_discount") or 0.0) / 100.0)
            price += (chosen.get("price_surcharge") or 0.0)
        result[p["id"]] = round(max(price, 0.0), 2)

    return result
