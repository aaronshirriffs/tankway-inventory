# MDR Inventory API — Getting Started

Welcome. This guide explains how to connect to the **MDR Inventory API** and
retrieve live stock availability using the API key we have issued to you.

> **Online version:** an always-current copy of this documentation is available at
> **https://api.mdrlighting.co.nz/docs**

---

## Your details

| | |
|---|---|
| **Base URL** | `https://api.mdrlighting.co.nz` |
| **Your API key** | `__________________________________________` *(provided separately)* |

> Keep your API key secret — it identifies your account and controls which
> products and stock you can see. Do not share it or embed it in public code.

---

## 1. Authentication

Every request to `/v1/products` must include your API key. Use **either** method:

**Option A — Bearer token header (recommended)**
```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
     https://api.mdrlighting.co.nz/v1/products
```

**Option B — query parameter**
```bash
curl "https://api.mdrlighting.co.nz/v1/products?key=YOUR_API_KEY"
```

---

## 2. Get products and stock — `GET /v1/products`

Returns the products visible to your key, each with current on-hand
availability grouped into the stock labels configured for your account.

### Response fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Stable product identifier |
| `name` | string | Product name |
| `sku` | string (nullable) | Product SKU / internal reference |
| `last_updated` | string | UTC timestamp the product was last modified |
| `sales_price` | number (optional) | Unit sales price — only present if pricing is enabled for your key |
| `stock` | object | Availability by label, e.g. `{ "Available Immediately": 12 }`. Values are summed on-hand quantities |

### Example response
```json
{
  "count": 2,
  "products": [
    {
      "id": 1423,
      "name": "Chauvet Maverick MK3 Spot",
      "sku": "CHV-MK3-SPOT",
      "last_updated": "2026-06-05 21:14:02",
      "sales_price": 4299.00,
      "stock": {
        "Available Immediately": 7,
        "On Request": 3
      }
    },
    {
      "id": 1610,
      "name": "Antari FT-200 Fog Machine",
      "sku": "ANT-FT200",
      "last_updated": "2026-06-04 09:32:51",
      "stock": {
        "Available Immediately": 0,
        "On Request": 15
      }
    }
  ]
}
```

---

## 3. Health check — `GET /v1/status` (no key required)

A lightweight endpoint for monitoring that the API is up:
```json
{
  "api": "MDR Inventory API",
  "status": "ok",
  "timestamp": "2026-06-06T14:21:09+12:00"
}
```
*(The timestamp is New Zealand time.)*

---

## 4. Error codes

| Status | Meaning | Body |
|--------|---------|------|
| `401` | Missing, invalid, disabled, or expired API key | `{ "error": "Invalid or expired API key" }` |
| `429` | Rate limit exceeded — retry after the indicated seconds | `{ "error": "Rate limit exceeded", "retry_after_seconds": 30 }` |
| `500` | Unexpected server error | `{ "error": "Internal server error" }` |

---

## 5. Rate limits

Your key has a **per-minute limit**, a short **burst allowance**, and a **daily
cap**. If you exceed a limit you will receive a `429` response with a
`retry_after_seconds` value telling you how long to wait.

**Please build a small retry/backoff into your client** so it pauses for
`retry_after_seconds` and then retries, rather than hammering the API.

### Example: polling politely (Python)
```python
import time, requests

URL = "https://api.mdrlighting.co.nz/v1/products"
HEADERS = {"Authorization": "Bearer YOUR_API_KEY"}

def get_products():
    while True:
        r = requests.get(URL, headers=HEADERS, timeout=30)
        if r.status_code == 429:
            wait = r.json().get("retry_after_seconds", 30)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()

data = get_products()
print(f"{data['count']} products")
for p in data["products"]:
    print(p["name"], p["stock"])
```

---

## 6. Tips

- **Cache results** on your side for a few minutes rather than calling on every
  page load — stock does not change second-to-second, and it keeps you well
  within your rate limits.
- **Match on `sku`** (or `id`) when syncing to your own catalogue; `id` is the
  most stable identifier.
- The set of products and the stock labels you see are configured for your
  account. If you need additional categories, pricing, or warehouses exposed,
  contact your account manager.

---

For key requests or support, contact your MDR Lighting account manager.
