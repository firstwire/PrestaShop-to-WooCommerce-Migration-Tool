#!/usr/bin/env python3
"""
PrestaShop → WooCommerce CSV Migration Tool
============================================
Converts PrestaShop exported CSVs (Products, Orders, Customers, Addresses)
into WooCommerce-compatible import CSVs.

Usage:
    python prestashop_to_woocommerce.py
    python prestashop_to_woocommerce.py --input ./input --output ./output
    python prestashop_to_woocommerce.py --file ./input/products.csv
    python prestashop_to_woocommerce.py --file input/ps_customers.csv --inspect

Field Mappings:
    Products  → id_product→ID, reference→SKU, ean13/upc/isbn/mpn→GTIN,
                image/image_url→Images, quantity→Stock, active→Published,
                price/price_tax_excl→Regular price, specific_price/reduction→Sale price,
                weight→Weight (kg), width/height/depth→dimensions (cm),
                manufacturer_name→Brands, id_product_attribute/combinations→variants
    Customers → firstname+lastname→first_name/last_name, email→user_email,
                phone/phone_mobile→billing_phone, address fields→billing/shipping
                (PrestaShop keeps addresses in a separate `address` export —
                this tool merges customers.csv + addresses.csv automatically
                if both are present, matched on id_customer)
    Orders    → id_order→order_id, current_state→status, reference→order_number,
                date_add→order_date, total_paid(_tax_incl)→order_total, etc.

Notes on PrestaShop exports:
    PrestaShop's back office "Export" (Catalog > Products, Customers > Addresses,
    Orders) produces CSVs with underscored, lowercase column names such as
    id_product, id_customer, id_order, current_state, total_paid_tax_incl.
    Some shops instead export directly from phpMyAdmin / a DB dump, which can
    carry mixed case or slightly different names (Reference, Quantity, Price).
    All lookups below are fuzzy-matched (case + whitespace + underscore
    insensitive) via the same `safe()`/`_norm()` helpers used in the
    OpenCart tool, so either style works.
"""

import os
import sys
import csv
import argparse
import re
from pathlib import Path
from datetime import datetime
from collections import defaultdict


# ─────────────────────────────────────────────────────────────
# DETECTION SIGNATURES
# Normalised (lowercase, no whitespace, no underscores) header
# matching so the detector works regardless of casing, spacing,
# or underscore variations (e.g. "id_product" == "IdProduct").
# ─────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Lowercase + strip whitespace AND underscores for fuzzy header matching."""
    return re.sub(r"[\s_]", "", s.lower())


def detect_file_type(headers: list[str]) -> str:
    """
    Return 'products', 'orders', 'customers', 'addresses', or 'unknown'.

    PrestaShop export column signatures:
      Products  → id_product, reference, price, quantity, active
      Orders    → id_order, current_state, total_paid, reference (invoice), payment
      Customers → id_customer, firstname, lastname, email, passwd
      Addresses → id_address, id_customer, address1, postcode
    """
    h = {_norm(c) for c in headers}

    # ── Addresses: id_address (+ id_customer) + address1 ───────
    has_addr_id  = h & {"idaddress"}
    has_addr_fld = h & {"address1", "address", "postcode", "zipcode"}
    if has_addr_id and has_addr_fld:
        return "addresses"

    # ── Orders: id_order + payment/status/total fields ─────────
    # Check orders BEFORE customers/products — orders can carry
    # firstname/lastname AND price-like totals too.
    has_order_id = h & {"idorder", "order#", "orderid"}
    has_order_fields = h & {"currentstate", "totalpaid", "totalpaidtaxincl",
                            "totalpaidtaxexcl", "payment", "invoicenumber",
                            "idcarrier", "totalshipping", "totalpaidreal",
                            "orderstate"}
    if has_order_id and has_order_fields:
        return "orders"

    # ── Orders (admin "Orders list" grid export variant) ────────
    # PrestaShop's back-office Orders grid export uses a much lighter,
    # human-readable column set: ID, Reference, New client, Delivery,
    # Customer, Total, Payment, Status, Date — no id_order/current_state.
    has_grid_order_fields = h & {"reference", "customer", "payment", "status"}
    has_total = h & {"total"}
    not_product_like = not (h & {"price", "quantity", "sku", "ean13"})
    not_customer_like = not (h & {"email", "passwd", "firstname", "lastname"})
    if (len(has_grid_order_fields) >= 3 and has_total
            and not_product_like and not_customer_like):
        return "orders"

    # ── Products: id_product/reference + price ──────────────────
    has_product = h & {"idproduct", "reference", "ean13", "upc", "isbn", "mpn"}
    has_price   = h & {"price", "pricetaxexcl", "pricetaxincl", "wholesaleprice"}
    if has_product and has_price:
        return "products"

    # ── Customers: id_customer + firstname + email ─────────────
    has_cust_id     = h & {"idcustomer", "idguest"}
    has_cust_fields = h & {"firstname", "lastname", "passwd", "newsletter",
                           "iddefaultgroup", "optin"}
    if (has_cust_id or has_cust_fields) and "email" in h:
        if not (h & {"idorder", "currentstate", "invoicenumber"}):
            return "customers"

    # ── Fallback: score each type ────────────────────────────────
    scores = {
        "products":  len(h & {"idproduct", "reference", "ean13", "price",
                               "quantity", "active", "weight",
                               "manufacturername", "image"}),
        "orders":    len(h & {"idorder", "currentstate", "totalpaid",
                              "totalpaidtaxincl", "payment", "invoicenumber",
                              "dateadd", "reference"}),
        "customers": len(h & {"idcustomer", "firstname", "lastname",
                              "email", "passwd", "newsletter", "dateadd"}),
        "addresses": len(h & {"idaddress", "idcustomer", "address1",
                              "postcode", "city", "phone"}),
    }
    best_type, best_score = max(scores.items(), key=lambda x: x[1])
    if best_score >= 3:
        return best_type

    return "unknown"


def safe(row: dict, *keys: str, default: str = "") -> str:
    """
    Get a value from a CSV row dict, trying multiple key spellings.
    Matching is exact first, then case+underscore+space insensitive.
    """
    norm_map: dict[str, str] = {_norm(k): v for k, v in row.items()}
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
        val = norm_map.get(_norm(key))
        if val not in (None, ""):
            return str(val).strip()
    return default


def yn_to_1_0(val: str) -> str:
    """Convert yes/no, true/false, 1/0, enabled/disabled to WooCommerce 1/0."""
    return "1" if str(val).lower() in {"yes", "true", "1", "on",
                                        "enabled", "active", "visible"} else "0"


def parse_price(val: str) -> str:
    """Strip currency symbols and return a clean decimal string."""
    cleaned = re.sub(r"[^\d.]", "", str(val))
    return cleaned if cleaned else "0"


def prestashop_status_to_woo(status: str) -> str:
    """
    Map PrestaShop order status (current_state, numeric ID or text name)
    → WooCommerce order status slug (no wc- prefix — importer adds it).

    PrestaShop default order states (id_order_state):
      1  = Awaiting check payment      → on-hold
      2  = Payment accepted            → processing
      3  = Processing in progress      → processing
      4  = Shipped                     → completed
      5  = Delivered                   → completed
      6  = Cancelled                   → cancelled
      7  = Refunded                    → refunded
      8  = Payment error               → failed
      9  = On backorder (paid)         → processing
      10 = Awaiting bank wire payment  → on-hold
      11 = Remote payment accepted     → processing
      12 = On backorder (not paid)     → on-hold
      13 = Awaiting Cash on Delivery   → on-hold
    """
    mapping = {
        # Numeric IDs
        "1":  "on-hold",
        "2":  "processing",
        "3":  "processing",
        "4":  "completed",
        "5":  "completed",
        "6":  "cancelled",
        "7":  "refunded",
        "8":  "failed",
        "9":  "processing",
        "10": "on-hold",
        "11": "processing",
        "12": "on-hold",
        "13": "on-hold",
        # Text names
        "awaiting check payment":       "on-hold",
        "payment accepted":             "processing",
        "processing in progress":       "processing",
        "shipped":                      "completed",
        "delivered":                    "completed",
        "cancelled":                    "cancelled",
        "canceled":                     "cancelled",
        "refunded":                     "refunded",
        "payment error":                "failed",
        "on backorder (paid)":          "processing",
        "on backorder":                 "on-hold",
        "awaiting bank wire payment":   "on-hold",
        "remote payment accepted":      "processing",
        "on backorder (not paid)":      "on-hold",
        "awaiting cash on delivery validation": "on-hold",
        "pending":                      "pending",
        "processing":                   "processing",
        "completed":                    "completed",
        "on hold":                      "on-hold",
        "failed":                       "failed",
    }
    return mapping.get(status.strip().lower(), "pending")


def format_date(val: str) -> str:
    """Try to parse various date formats and return YYYY-MM-DD HH:MM:SS."""
    if not val:
        return ""
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(val.strip(), fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return val


def split_name(full_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last)."""
    parts = full_name.strip().split(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (full_name, "")


def prestashop_active_to_published(status: str) -> str:
    """PrestaShop product/customer 'active' → WooCommerce Published (1/0)."""
    return "1" if str(status).strip().lower() in {"1", "enabled", "true", "yes"} else "0"


# ─────────────────────────────────────────────────────────────
# id_country / id_state → ISO CODE TRANSLATION
#
# PrestaShop's address exports (and the ps_address table itself)
# store `id_country` and `id_state` as internal numeric database IDs
# (foreign keys into ps_country / ps_state), NOT country/state names
# or ISO codes. WooCommerce, on the other hand, requires a real
# ISO 3166-1 alpha-2 country code (e.g. "US", "FR", "IN") in
# billing_country/shipping_country — a bare number like "21" is
# invalid, so WooCommerce silently drops it, which is why Country /
# Region (and often City / Postal code alongside it) show up blank
# in WooCommerce Analytics after import.
#
# THESE IDS ARE NOT UNIVERSAL — every PrestaShop install can have a
# different id_country/id_state table depending on which countries/
# states were enabled, deleted, or re-ordered by the store owner.
# The two entries below (8 → FR, 21 → US) were confirmed directly
# from this store's own data (city names "Paris" / "New York" /
# "Miami" / "St. Louis" matched those IDs). Add any other IDs your
# store uses by running this in phpMyAdmin / SQL Manager and copying
# the results in below:
#
#   SELECT id_country, iso_code FROM ps_country;
#   SELECT id_state, iso_code FROM ps_state;
#
COUNTRY_ID_TO_ISO: dict[str, str] = {
    "8":  "FR",   # France   — confirmed from this store's data (Paris)
    "21": "US",   # United States — confirmed from this store's data (New York/Miami/St. Louis)
    # Add more rows here as "id": "ISO", e.g. "10": "IT",
}

STATE_ID_TO_CODE: dict[str, str] = {
    "0":  "",     # 0 = no state (used for countries without states, e.g. France)
    # NOTE: unlike COUNTRY_ID_TO_ISO above, we deliberately do NOT hardcode
    # guesses for other id_state values here. PrestaShop's internal state
    # IDs are not a fixed universal table — they depend on which states
    # were enabled/deleted on this specific store, so a guessed mapping
    # (e.g. "28 = Missouri") could easily be wrong on a different install
    # and would silently mislabel a customer's real state. Instead, the
    # city-name lookup below (CITY_TO_LOCATION) is used as a safer,
    # independently-verifiable fallback.
}

# ─────────────────────────────────────────────────────────────
# CITY NAME → (STATE CODE, COUNTRY CODE) FALLBACK LOOKUP
#
# When id_country / id_state can't be resolved from the small verified
# map above, we fall back to recognising the city name itself — since
# "Miami" is reliably Florida/US and "Paris" is reliably France, this
# is independently checkable geography rather than a guess about a
# specific store's internal database IDs. Only well-known, unambiguous
# cities are included. Extend this dict with more of your store's
# cities if needed (format: "cityname": ("STATE_CODE", "COUNTRY_CODE"),
# use "" for STATE_CODE if the country has no state/province system).
# ─────────────────────────────────────────────────────────────
CITY_TO_LOCATION: dict[str, tuple[str, str]] = {
    "paris":         ("", "FR"),
    "marseille":     ("", "FR"),
    "lyon":          ("", "FR"),
    "new york":      ("NY", "US"),
    "miami":         ("FL", "US"),
    "st. louis":     ("MO", "US"),
    "saint louis":   ("MO", "US"),
    "los angeles":   ("CA", "US"),
    "chicago":       ("IL", "US"),
    "houston":       ("TX", "US"),
    "phoenix":       ("AZ", "US"),
    "philadelphia":  ("PA", "US"),
    "san antonio":   ("TX", "US"),
    "san diego":     ("CA", "US"),
    "dallas":        ("TX", "US"),
    "san jose":      ("CA", "US"),
    "austin":        ("TX", "US"),
    "boston":        ("MA", "US"),
    "seattle":       ("WA", "US"),
    "denver":        ("CO", "US"),
    "atlanta":       ("GA", "US"),
    "london":        ("", "GB"),
    "manchester":    ("", "GB"),
    "berlin":        ("", "DE"),
    "munich":        ("", "DE"),
    "madrid":        ("", "ES"),
    "barcelona":     ("", "ES"),
    "rome":          ("", "IT"),
    "milan":         ("", "IT"),
    "toronto":       ("ON", "CA"),
    "vancouver":     ("BC", "CA"),
    "mumbai":        ("MH", "IN"),
    "delhi":         ("DL", "IN"),
    "new delhi":     ("DL", "IN"),
    "bangalore":     ("KA", "IN"),
    "bengaluru":     ("KA", "IN"),
    "lucknow":       ("UP", "IN"),
    "sydney":        ("NSW", "AU"),
    "melbourne":     ("VIC", "AU"),
}


def guess_location_from_city(city: str) -> tuple[str, str] | None:
    """
    Fallback: recognise a well-known city name and return (state, country).
    Returns None if the city isn't in our lookup — callers should keep
    whatever raw value they already had rather than blanking it out.
    (A recognised city with no state system, e.g. Paris, correctly
    returns ("", "FR") rather than None.)
    """
    key = (city or "").strip().lower()
    return CITY_TO_LOCATION.get(key)


def translate_country(val: str, city: str = "") -> str:
    """
    Convert a PrestaShop id_country (or already-valid ISO code / name)
    into a WooCommerce-compatible ISO 3166-1 alpha-2 country code.
    Falls back to a city-name lookup if the ID isn't in our verified map.
    """
    val = (val or "").strip()
    if not val:
        return ""
    if val.isdigit():
        if val in COUNTRY_ID_TO_ISO:
            return COUNTRY_ID_TO_ISO[val]
        match = guess_location_from_city(city)
        if match is not None:
            return match[1]  # country — may legitimately be "" only if unset
        return val  # no match at all — keep raw ID so it's visibly a TODO
    # Already looks like an ISO code (2 letters) — keep as-is.
    if len(val) == 2 and val.isalpha():
        return val.upper()
    return val


def translate_state(val: str, city: str = "") -> str:
    """
    Convert a PrestaShop id_state (or already-valid state code / name)
    into a WooCommerce-compatible state code.
    Falls back to a city-name lookup if the ID isn't in our verified map.
    """
    val = (val or "").strip()
    if not val:
        return ""
    if val.isdigit():
        if val in STATE_ID_TO_CODE:
            return STATE_ID_TO_CODE[val]
        match = guess_location_from_city(city)
        if match is not None:
            return match[0]  # state — correctly "" for stateless countries
        return val  # no match at all — keep raw ID so it's visibly a TODO
    return val


def convert_weight_to_kg(val: str, unit: str = "") -> str:
    """Convert weight from various units to kg for WooCommerce."""
    try:
        w = float(parse_price(val))
    except (ValueError, TypeError):
        return val
    unit = unit.strip().lower()
    if unit in {"lb", "lbs", "pound", "pounds"}:
        return f"{w * 0.453592:.4f}"
    if unit in {"g", "gram", "grams"}:
        return f"{w / 1000:.4f}"
    if unit in {"oz", "ounce", "ounces"}:
        return f"{w * 0.0283495:.4f}"
    return f"{w:.4f}" if w else ""


def convert_dim_to_cm(val: str, unit: str = "") -> str:
    """Convert dimensions from various units to cm for WooCommerce."""
    try:
        d = float(parse_price(val))
    except (ValueError, TypeError):
        return val
    unit = unit.strip().lower()
    if unit in {"in", "inch", "inches"}:
        return f"{d * 2.54:.4f}"
    if unit in {"mm", "millimeter", "millimeters"}:
        return f"{d / 10:.4f}"
    if unit in {"ft", "feet", "foot"}:
        return f"{d * 30.48:.4f}"
    return f"{d:.4f}" if d else ""


# ═════════════════════════════════════════════════════════════
# PRODUCTS CONVERSION
# ═════════════════════════════════════════════════════════════
#
# PrestaShop → WooCommerce field mapping:
#   id_product                 → ID
#   reference                  → SKU
#   ean13 / upc / isbn / mpn   → GTIN, UPC, EAN, or ISBN
#   image / image_url          → Images
#   quantity                   → Stock
#   active                     → Published
#   price / price_tax_excl     → Regular price
#   specific_price / reduction → Sale price
#   weight                     → Weight (kg)
#   width/height/depth         → Width/Height/Length (cm)
#   position_in_category       → Position
#   id_category_default/category → Categories
#   manufacturer_name          → Brands
#   id_product_attribute       → variant grouping (combinations)
#   condition                  → Meta: condition
#   available_for_order        → In stock override
#
WOO_PRODUCT_COLS = [
    "ID", "Type", "SKU", "Name", "Published", "Is featured?",
    "Visibility in catalog", "Short description", "Description",
    "Date sale price starts", "Date sale price ends",
    "Tax status", "Tax class", "In stock?", "Stock",
    "Low stock amount", "Backorders allowed?", "Sold individually?",
    "Weight (kg)", "Length (cm)", "Width (cm)", "Height (cm)",
    "Allow customer reviews?", "Purchase note", "Sale price",
    "Regular price", "Categories", "Tags", "Shipping class",
    "Images", "Download limit", "Download expiry days",
    "Parent", "Grouped products", "Upsells", "Cross-sells",
    "External URL", "Button text", "Position",
    "Brands",
    "GTIN, UPC, EAN, or ISBN",
    "Attribute 1 name", "Attribute 1 value(s)",
    "Attribute 1 visible", "Attribute 1 global",
    "Attribute 2 name", "Attribute 2 value(s)",
    "Attribute 2 visible", "Attribute 2 global",
    "Attribute 3 name", "Attribute 3 value(s)",
    "Attribute 3 visible", "Attribute 3 global",
    "Meta: _wc_average_rating", "Meta: total_sales",
    "Meta: MPN",
    "Meta: condition",
]


def convert_products(rows: list[dict]) -> list[dict]:
    """
    Convert PrestaShop product rows → WooCommerce product rows.

    PrestaShop export column reference (Catalog > Products export, or
    a `ps_product` + `ps_product_lang` + `ps_stock_available` join):
      id_product, reference, supplier_reference, ean13, upc, isbn, mpn,
      quantity, active, id_category_default, category, image, image_url,
      id_manufacturer, manufacturer_name, price, price_tax_excl,
      wholesale_price, specific_price, reduction, tax_rule_group,
      weight, width, height, depth, condition, available_for_order,
      visibility, name, description, description_short, tags,
      id_product_attribute, attribute_name, attribute_value
    """
    # Group by id_product (combinations/variants share the same id_product
    # but differ by id_product_attribute)
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        product_id = safe(row, "id_product", "IdProduct", "product_id", "id", "ID")
        groups[product_id].append(row)

    out_rows: list[dict] = []

    for group_key, variants in groups.items():
        first = variants[0]

        # A group has real variants only if more than one row AND the rows
        # differ by a combination id (id_product_attribute != 0/empty)
        combo_ids = {safe(v, "id_product_attribute", "IdProductAttribute",
                          "combination_id") for v in variants}
        has_variants = len(variants) > 1 and len(combo_ids - {"", "0"}) > 0
        product_type = "variable" if has_variants else "simple"

        # ── SKU: prefer reference ───────────────────────────
        sku = (safe(first, "reference", "Reference", "sku", "SKU") or
               safe(first, "supplier_reference", "SupplierReference"))

        # ── GTIN: ean13 → upc → isbn → mpn ───────────────────
        gtin = (safe(first, "ean13", "EAN13", "ean") or
                safe(first, "upc", "UPC") or
                safe(first, "isbn", "ISBN") or
                safe(first, "mpn", "MPN"))

        # ── Name ─────────────────────────────────────────────
        name = (safe(first, "name", "Name", "product_name") or
                sku or f"Product {group_key}")

        # ── Published: active=1 → 1 ─────────────────────────
        published = prestashop_active_to_published(
            safe(first, "active", "Active", "status")
        )

        # ── Stock ────────────────────────────────────────────
        stock    = safe(first, "quantity", "Quantity", "stock", "qty")
        avail_for_order = safe(first, "available_for_order", "AvailableForOrder")
        in_stock = "1" if (stock and stock != "0") or avail_for_order in {"1", "yes", "true"} else "0"

        # ── Tax status ───────────────────────────────────────
        tax_rule = safe(first, "tax_rule_group", "TaxRuleGroup", "id_tax_rules_group")
        tax_status = "none" if tax_rule in {"0", "", "no tax", "none"} else "taxable"

        # ── Price ────────────────────────────────────────────
        regular_price = parse_price(
            safe(first, "price_tax_excl", "price", "Price", "PriceTaxExcl")
        )
        sale_price = parse_price(
            safe(first, "specific_price", "reduction", "sale_price",
                 "reduction_price", "SpecificPrice")
        )
        if sale_price == "0":
            sale_price = ""

        # ── Weight & dimensions (PrestaShop stores kg/cm by default) ──
        weight = convert_weight_to_kg(safe(first, "weight", "Weight"))
        length = convert_dim_to_cm(safe(first, "depth", "Depth", "length", "Length"))
        width  = convert_dim_to_cm(safe(first, "width",  "Width"))
        height = convert_dim_to_cm(safe(first, "height", "Height"))

        # ── Other fields ─────────────────────────────────────
        image       = safe(first, "image", "Image", "image_url", "cover_image")
        description = safe(first, "description", "Description")
        short_desc  = safe(first, "description_short", "DescriptionShort",
                           "short_description", "Short Description")
        categories  = safe(first, "category", "categories", "Category",
                           "category_default", "id_category_default")
        tags        = safe(first, "tags", "Tags", "keywords")
        brand       = safe(first, "manufacturer_name", "ManufacturerName",
                           "manufacturer", "Manufacturer", "brand", "Brand")
        position    = safe(first, "position_in_category", "position",
                           "PositionInCategory")
        condition   = safe(first, "condition", "Condition")

        parent: dict = {col: "" for col in WOO_PRODUCT_COLS}
        parent.update({
            "ID":                       group_key,
            "Type":                     product_type,
            "SKU":                      sku,
            "Name":                     name,
            "Published":                published,
            "Is featured?":             "0",
            "Visibility in catalog":    "visible",
            "Short description":        short_desc,
            "Description":              description,
            "Tax status":               tax_status,
            "Tax class":                "",
            "In stock?":                in_stock,
            "Stock":                    stock,
            "Backorders allowed?":      "0",
            "Sold individually?":       "0",
            "Allow customer reviews?":  "1",
            "Regular price":            regular_price,
            "Sale price":               sale_price,
            "Categories":               categories,
            "Tags":                     tags,
            "Images":                   image,
            "Weight (kg)":              weight,
            "Length (cm)":              length,
            "Width (cm)":               width,
            "Height (cm)":              height,
            "Position":                 position,
            "Brands":                   brand,
            "GTIN, UPC, EAN, or ISBN":  gtin,
            "Meta: condition":          condition,
        })

        # ── Variant attributes (PrestaShop combinations) ─────
        if has_variants:
            all_opt_names: list[str] = []
            for i in range(1, 4):
                opt_name = safe(first,
                                f"attribute{i}name", f"Attribute{i} Name",
                                f"option{i}name")
                if opt_name:
                    all_opt_names.append(opt_name)

            # Fallback: PrestaShop combination exports often just have a
            # single "attribute_name" / "attribute_value" pair per row
            # (e.g. Size, Color) rather than numbered columns.
            if not all_opt_names:
                generic_name = safe(first, "attribute_name", "AttributeName",
                                    "group_name")
                if generic_name:
                    all_opt_names.append(generic_name)
                    vals = list({
                        safe(v, "attribute_value", "AttributeValue", "option_value")
                        for v in variants
                    })
                    parent["Attribute 1 name"]     = generic_name
                    parent["Attribute 1 value(s)"] = ", ".join(filter(None, vals))
                    parent["Attribute 1 visible"]  = "1"
                    parent["Attribute 1 global"]   = "1"
            else:
                for idx, opt_name in enumerate(all_opt_names, 1):
                    vals = list({
                        safe(v, f"attribute{idx}value", f"Attribute{idx} Value",
                             f"option{idx}value")
                        for v in variants
                    })
                    parent[f"Attribute {idx} name"]     = opt_name
                    parent[f"Attribute {idx} value(s)"] = ", ".join(filter(None, vals))
                    parent[f"Attribute {idx} visible"]  = "1"
                    parent[f"Attribute {idx} global"]   = "1"

            # Sum stock across variants
            total_stock = 0
            for v in variants:
                try:
                    total_stock += int(safe(v, "quantity", "Quantity", "qty") or 0)
                except ValueError:
                    pass
            parent["Stock"] = str(total_stock) if total_stock > 0 else ""

            # Lowest price as base price
            prices = []
            for v in variants:
                p = parse_price(safe(v, "price_tax_excl", "price", "Price"))
                try:
                    prices.append(float(p))
                except ValueError:
                    pass
            if prices:
                parent["Regular price"] = str(min(prices))

        out_rows.append(parent)

    return out_rows


# ═════════════════════════════════════════════════════════════
# ADDRESSES HELPER (PrestaShop keeps addresses in their own table)
# ═════════════════════════════════════════════════════════════

def index_addresses(rows: list[dict]) -> dict[str, dict]:
    """
    Build a lookup {id_customer: address_row} from a PrestaShop
    addresses.csv export. If a customer has multiple addresses,
    the first one encountered is used as billing/shipping default.
    """
    idx: dict[str, dict] = {}
    for row in rows:
        cust_id = safe(row, "id_customer", "IdCustomer", "customer_id")
        if cust_id and cust_id not in idx:
            idx[cust_id] = row
    return idx


# ═════════════════════════════════════════════════════════════
# CUSTOMERS CONVERSION
# ═════════════════════════════════════════════════════════════
#
# Field mapping (PrestaShop → WooCommerce):
#   id_customer     → customer_id
#   firstname       → first_name, billing_first_name, shipping_first_name
#   lastname        → last_name, billing_last_name, shipping_last_name
#   email           → user_email, billing_email
#   phone/phone_mobile → billing_phone, shipping_phone (from address row)
#   company         → billing_company, shipping_company (from address row)
#   address1/address2 → billing/shipping address lines (from address row)
#   city            → billing_city, shipping_city (from address row)
#   postcode        → billing_postcode, shipping_postcode (from address row)
#   id_state/state  → billing_state, shipping_state (from address row)
#   id_country/country → billing_country, shipping_country (from address row)
#   date_add        → user_registered
#   active          → user_status (PrestaShop 1=active → WP 0=active, inverted)
#   passwd          → user_pass
#   newsletter/optin → description
#
WOO_CUSTOMER_COLS = [
    "customer_id",
    "user_email",
    "user_login",
    "user_pass",
    "user_registered",
    "user_status",
    "roles",
    "wp_capabilities",
    "first_name",
    "last_name",
    "description",
    # Standard WebToffee billing fields
    "billing_first_name",
    "billing_last_name",
    "billing_company",
    "billing_address_1",
    "billing_address_2",
    "billing_city",
    "billing_state",
    "billing_postcode",
    "billing_country",
    "billing_email",
    "billing_phone",
    # Standard WebToffee shipping fields
    "shipping_first_name",
    "shipping_last_name",
    "shipping_company",
    "shipping_address_1",
    "shipping_address_2",
    "shipping_city",
    "shipping_state",
    "shipping_postcode",
    "shipping_country",
    "shipping_phone",
    # meta: prefix = WebToffee writes DIRECTLY to wp_usermeta.
    # Ensures Country/Region, City, Region, Postal code appear in
    # WooCommerce Analytics > Customers immediately after import
    # with NO manual sync or cache clear needed.
    "meta:billing_country",
    "meta:billing_city",
    "meta:billing_state",
    "meta:billing_postcode",
    "meta:billing_address_1",
    "meta:billing_address_2",
    "meta:billing_first_name",
    "meta:billing_last_name",
    "meta:billing_company",
    "meta:billing_email",
    "meta:billing_phone",
    "meta:shipping_country",
    "meta:shipping_city",
    "meta:shipping_state",
    "meta:shipping_postcode",
    "meta:shipping_address_1",
    "meta:shipping_address_2",
    "meta:shipping_first_name",
    "meta:shipping_last_name",
    "meta:shipping_company",
    "meta:shipping_phone",
]


def convert_customers(rows: list[dict], addresses_by_customer: dict[str, dict] | None = None) -> list[dict]:
    """
    Convert PrestaShop customer rows → WooCommerce customer rows.

    PrestaShop export column reference:
      id_customer, id_gender, firstname, lastname, email, passwd,
      birthday, newsletter, optin, company, siret, ape,
      id_default_group, active, date_add

    If `addresses_by_customer` is supplied (from a separate PrestaShop
    addresses.csv export, matched on id_customer), billing/shipping
    address fields are pulled from there — PrestaShop does not store
    addresses on the customer record itself.
    """
    addresses_by_customer = addresses_by_customer or {}
    out_rows: list[dict] = []

    for idx, row in enumerate(rows, 1):

        # ── customer_id ─────────────────────────────────────
        customer_id = safe(row, "id_customer", "IdCustomer", "customer_id", "id", "ID")

        # ── email ───────────────────────────────────────────
        email = safe(row, "email", "Email", "email_address", "EmailAddress")
        if not email:
            for v in row.values():
                v = str(v or "").strip()
                if "@" in v and "." in v.split("@")[-1]:
                    email = v
                    break

        if not email:
            print(f"    ⚠  Row {idx}: no email found — skipped. "
                  f"(columns: {list(row.keys())[:6]} ...)")
            continue

        # ── firstname / lastname ─────────────────────────────
        first_name = safe(row, "firstname", "first_name", "FirstName",
                          "first name", "given_name")
        last_name  = safe(row, "lastname",  "last_name",  "LastName",
                          "last name", "surname", "family_name")

        if not first_name and not last_name:
            full = safe(row, "name", "full_name", "customer_name")
            first_name, last_name = split_name(full)

        if not first_name and not last_name:
            local = email.split("@")[0]
            parts = local.replace(".", " ").replace("_", " ").replace("-", " ").split()
            first_name = parts[0].capitalize() if parts else ""
            last_name  = " ".join(p.capitalize() for p in parts[1:]) if len(parts) > 1 else ""

        username  = email.split("@")[0]
        user_pass = safe(row, "passwd", "password", "Password", "pass")

        user_registered = format_date(
            safe(row, "date_add", "DateAdd", "created_at", "registration_date")
        )

        # PrestaShop active: 1=active → WordPress user_status: 0=active (inverted)
        ps_active   = safe(row, "active", "Active", "status")
        user_status = "0" if ps_active in {"1", "enabled", "active", "true"} else "1"

        # ── Address lookup (from separate addresses export, if any) ──
        addr_row = addresses_by_customer.get(customer_id, {})

        company   = safe(row, "company", "Company") or safe(addr_row, "company", "Company")
        telephone = (safe(addr_row, "phone", "Phone") or
                     safe(addr_row, "phone_mobile", "PhoneMobile") or
                     safe(row, "phone", "Phone"))

        addr1    = safe(addr_row, "address1", "Address1", "address_1", "street_address")
        addr2    = safe(addr_row, "address2", "Address2", "address_2")
        city     = safe(addr_row, "city", "City")
        postcode = safe(addr_row, "postcode", "Postcode", "postal_code", "zip", "zip_code")
        state    = translate_state(
            safe(addr_row, "state", "State", "id_state", "province", "region"), city
        )
        country  = translate_country(
            safe(addr_row, "country", "Country", "id_country", "country_code"), city
        )

        # Separate shipping address (PrestaShop's basic export usually has
        # only one address per customer — falls back to billing)
        ship_addr1    = safe(addr_row, "shipping_address_1", "ship_address1") or addr1
        ship_addr2    = safe(addr_row, "shipping_address_2", "ship_address2") or addr2
        ship_city     = safe(addr_row, "shipping_city",      "ship_city")     or city
        ship_state    = safe(addr_row, "shipping_state")                     or state
        ship_postcode = safe(addr_row, "shipping_postcode",  "ship_postcode") or postcode
        ship_country  = safe(addr_row, "shipping_country",   "ship_country")  or country
        ship_company  = safe(addr_row, "shipping_company",   "ship_company") or company

        newsletter = safe(row, "newsletter", "Newsletter")
        optin      = safe(row, "optin", "Optin")
        desc = "Newsletter: Yes" if newsletter in {"1", "yes", "true"} or optin in {"1", "yes", "true"} else ""

        # ── Role determination ───────────────────────────────
        # KEY RULE: Newsletter/optin flag is an EMAIL PREFERENCE, not a role.
        # Anyone with an id_customer IS a customer.
        # Only assign "subscriber" if id_default_group is explicitly a
        # guest/visitor group (meaning they never actually purchased).
        total_orders = safe(row, "total_orders", "order_count", "orders",
                            default="0")
        total_spent  = parse_price(safe(row, "total_spent", "total_paid_real",
                                        "lifetime_value", default="0"))

        try:
            has_orders = int(total_orders) > 0
        except ValueError:
            has_orders = bool(total_orders and total_orders not in {"0", ""})

        try:
            has_spent = float(total_spent) > 0
        except ValueError:
            has_spent = bool(total_spent and total_spent not in {"0", ""})

        cust_group = safe(row, "id_default_group", "default_group",
                          "group_name", "customer_group").lower().strip()

        SUBSCRIBER_GROUPS = {"guest", "visitor", "subscriber", "newsletter",
                             "newsletter subscriber"}

        if has_orders or has_spent:
            woo_role = "customer"
        elif cust_group and cust_group in SUBSCRIBER_GROUPS:
            woo_role = "subscriber"
        else:
            woo_role = "customer"

        if woo_role == "customer":
            wp_caps = 'a:1:{s:8:"customer";b:1;}'
        else:
            wp_caps = 'a:1:{s:10:"subscriber";b:1;}'

        out: dict = {col: "" for col in WOO_CUSTOMER_COLS}
        out.update({
            "customer_id":          customer_id,
            "user_email":           email,
            "user_login":           username,
            "user_pass":            user_pass,
            "user_registered":      user_registered,
            "user_status":          user_status,
            "roles":                woo_role,
            "wp_capabilities":      wp_caps,
            "first_name":           first_name,
            "last_name":            last_name,
            "description":          desc,
            "billing_first_name":   first_name,
            "billing_last_name":    last_name,
            "billing_company":      company,
            "billing_address_1":    addr1,
            "billing_address_2":    addr2,
            "billing_city":         city,
            "billing_state":        state,
            "billing_postcode":     postcode,
            "billing_country":      country,
            "billing_email":        email,
            "billing_phone":        telephone,
            "shipping_first_name":  first_name,
            "shipping_last_name":   last_name,
            "shipping_company":     ship_company,
            "shipping_address_1":   ship_addr1,
            "shipping_address_2":   ship_addr2,
            "shipping_city":        ship_city,
            "shipping_state":       ship_state,
            "shipping_postcode":    ship_postcode,
            "shipping_country":     ship_country,
            "shipping_phone":       telephone,
            "meta:billing_country":      country,
            "meta:billing_city":         city,
            "meta:billing_state":        state,
            "meta:billing_postcode":     postcode,
            "meta:billing_address_1":    addr1,
            "meta:billing_address_2":    addr2,
            "meta:billing_first_name":   first_name,
            "meta:billing_last_name":    last_name,
            "meta:billing_company":      company,
            "meta:billing_email":        email,
            "meta:billing_phone":        telephone,
            "meta:shipping_country":     ship_country,
            "meta:shipping_city":        ship_city,
            "meta:shipping_state":       ship_state,
            "meta:shipping_postcode":    ship_postcode,
            "meta:shipping_address_1":   ship_addr1,
            "meta:shipping_address_2":   ship_addr2,
            "meta:shipping_first_name":  first_name,
            "meta:shipping_last_name":   last_name,
            "meta:shipping_company":     ship_company,
            "meta:shipping_phone":       telephone,
        })
        out_rows.append(out)

    return out_rows


# ═════════════════════════════════════════════════════════════
# ORDERS CONVERSION
# ═════════════════════════════════════════════════════════════
#
# Field mapping (PrestaShop → WooCommerce):
#   id_order                   → order_id
#   reference                  → order_number
#   current_state              → status
#   date_add                   → order_date
#   id_customer                → customer_id
#   email                      → customer_email / billing_email
#   payment                    → payment_method
#   id_carrier / carrier       → shipping_lines (method_title)
#   total_paid_tax_excl        → order_subtotal
#   total_shipping(_tax_incl)  → shipping_total
#   total_paid_tax_incl - total_paid_tax_excl → tax_total (approx)
#   total_paid(_real)          → order_total
#   product_name/product_quantity/product_price/total_price_tax_incl → line item
#
WOO_ORDER_COLS = [
    "order_id",
    "order_number",
    "status",
    "order_date",
    "paid_date",
    "customer_id",
    "customer_email",
    "payment_method",
    "payment_method_title",
    "transaction_id",
    "order_currency",
    "billing_first_name",
    "billing_last_name",
    "billing_company",
    "billing_address_1",
    "billing_address_2",
    "billing_city",
    "billing_state",
    "billing_postcode",
    "billing_country",
    "billing_email",
    "billing_phone",
    "shipping_first_name",
    "shipping_last_name",
    "shipping_company",
    "shipping_address_1",
    "shipping_address_2",
    "shipping_city",
    "shipping_state",
    "shipping_postcode",
    "shipping_country",
    "shipping_phone",
    "order_subtotal",
    "shipping_total",
    "tax_total",
    "order_total",
    # WebToffee line_items format:
    # name:X|qty:X|price:X|subtotal:X|total:X|subtotal_tax:X|total_tax:X|sku:X
    "line_items",
    # WebToffee shipping_lines format: method_title:X|cost:X
    "shipping_lines",
    "coupon_code",
    "customer_note",
    "order_notes",
]


def convert_orders(rows: list[dict]) -> list[dict]:
    """Convert PrestaShop order rows to WooCommerce order rows."""
    out_rows: list[dict] = []

    for row in rows:

        # ── Order identity ──────────────────────────────────
        order_id  = safe(row, "id_order", "IdOrder", "order_id", "id", "ID")
        order_num = safe(row, "reference", "Reference", "invoice_number",
                         "order_number") or order_id

        status_raw = safe(row, "current_state", "CurrentState", "order_state",
                          "status", "Status")
        status = prestashop_status_to_woo(status_raw)

        order_date = format_date(
            safe(row, "date_add", "DateAdd", "order_date", "created_at", "Date")
        )
        paid_date = order_date

        currency = (safe(row, "currency", "Currency", "id_currency") or "USD")

        payment = safe(row, "payment", "Payment", "payment_method") or "other"

        # carrier/id_carrier → shipping_lines, NOT a standalone column
        # "Delivery" in the admin grid export is actually the delivery
        # country, not a carrier name — used as shipping_country below.
        ship_method  = safe(row, "carrier", "Carrier", "id_carrier")
        grid_country = safe(row, "delivery", "Delivery")

        customer_id = safe(row, "id_customer", "IdCustomer", "customer_id")
        bill_email  = safe(row, "email", "Email", "customer_email")
        bill_phone  = safe(row, "phone", "Phone", "phone_mobile")

        # ── Billing name ─────────────────────────────────────
        # PrestaShop's full order exports carry firstname/lastname
        # directly on the row. The lighter admin "Orders list" grid
        # export instead gives a single "Customer" column formatted
        # as "F. Lastname" (first-initial + a dot + last name), e.g.
        # "A. mishra" — handled as a special case below.
        bill_first = safe(row, "firstname", "billing_firstname",
                          "first_name", "customer_firstname")
        bill_last  = safe(row, "lastname", "billing_lastname",
                          "last_name", "customer_lastname")

        if not bill_first and not bill_last:
            full = safe(row, "customer", "Customer", "customer_name", "full_name", "name")
            m = re.match(r"^([A-Za-z])\.\s+(.+)$", full)
            if m:
                # "A. mishra" → first initial "A", last name "mishra"
                bill_first, bill_last = m.group(1), m.group(2)
            elif full:
                bill_first, bill_last = split_name(full)



        bill_company  = safe(row, "company", "Company")
        bill_addr1    = safe(row, "address1", "billing_address_1", "address_1")
        bill_addr2    = safe(row, "address2", "billing_address_2", "address_2")
        bill_city     = safe(row, "city", "billing_city")
        bill_state    = translate_state(safe(row, "state", "billing_state", "id_state"), bill_city)
        bill_postcode = safe(row, "postcode", "billing_postcode", "zip_code")
        bill_country  = (translate_country(safe(row, "country", "billing_country", "id_country"), bill_city)
                         or grid_country)

        # ── Shipping address (fallback to billing) ───────────
        ship_first    = safe(row, "shipping_firstname", "shipping_first_name") or bill_first
        ship_last     = safe(row, "shipping_lastname",  "shipping_last_name")  or bill_last
        ship_company  = safe(row, "shipping_company")                          or bill_company
        ship_addr1    = safe(row, "shipping_address_1", "shipping_address1")   or bill_addr1
        ship_addr2    = safe(row, "shipping_address_2", "shipping_address2")   or bill_addr2
        ship_city     = safe(row, "shipping_city")                             or bill_city
        ship_state    = safe(row, "shipping_state")                            or bill_state
        ship_postcode = safe(row, "shipping_postcode", "shipping_zip")         or bill_postcode
        ship_country  = safe(row, "shipping_country")                          or bill_country
        ship_phone    = safe(row, "shipping_phone")                            or bill_phone

        # ── Totals ─────────────────────────────────────────────
        shipping = parse_price(safe(row, "total_shipping_tax_incl",
                                    "total_shipping", "shipping_cost", "shipping"))
        total_incl = parse_price(safe(row, "total_paid_tax_incl", "total_paid",
                                      "total", "Total"))
        total_excl = parse_price(safe(row, "total_paid_tax_excl", "total_tax_excl"))
        try:
            tax = str(round(float(total_incl) - float(total_excl), 2)) if total_excl else "0"
        except (ValueError, TypeError):
            tax = parse_price(safe(row, "total_tax", "tax"))
        total = total_incl or total_excl

        # ── Line item ───────────────────────────────────────────
        item_name = (safe(row, "product_name", "ProductName") or
                     safe(row, "product", "Product") or
                     safe(row, "item_name", "name", "Name"))

        item_sku = safe(row, "product_reference", "reference_product",
                        "product_sku", "sku", "SKU")

        item_qty = safe(row, "product_quantity", "quantity", "qty",
                        "Quantity", default="1")

        item_price = parse_price(safe(row, "product_price", "unit_price_tax_incl",
                                      "unit_price", "price", "Price"))

        item_total_raw = safe(row, "total_price_tax_incl", "product_total",
                              "line_total", "item_total")
        if item_total_raw:
            item_total = parse_price(item_total_raw)
        else:
            try:
                item_total = str(round(float(item_price) * float(item_qty), 2))
            except (ValueError, TypeError):
                item_total = item_price

        item_tax = parse_price(safe(row, "product_tax", "item_tax", "line_tax"))

        subtotal_raw = safe(row, "total_paid_tax_excl", "subtotal", "sub_total")
        order_subtotal = parse_price(subtotal_raw) if subtotal_raw else item_total

        # ── WebToffee line_items format ─────────────────────────
        line_items_str = (
            f"name:{item_name}|"
            f"qty:{item_qty}|"
            f"price:{item_price}|"
            f"subtotal:{item_total}|"
            f"total:{item_total}|"
            f"subtotal_tax:{item_tax}|"
            f"total_tax:{item_tax}|"
            f"sku:{item_sku}"
        )

        # ── WebToffee shipping_lines format ─────────────────────
        if ship_method and shipping and shipping != "0":
            shipping_lines_str = f"method_title:{ship_method}|cost:{shipping}"
        elif ship_method:
            shipping_lines_str = f"method_title:{ship_method}|cost:0"
        elif shipping and shipping != "0":
            shipping_lines_str = f"method_title:Shipping|cost:{shipping}"
        else:
            shipping_lines_str = ""

        coupon    = safe(row, "coupon_code", "coupon", "discount_code", "voucher")
        cust_note = safe(row, "comment", "comments", "customer_note", "order_comment")
        order_note = safe(row, "order_note", "staff_note", "admin_note")

        out: dict = {col: "" for col in WOO_ORDER_COLS}
        out.update({
            "order_id":             order_id,
            "order_number":         order_num,
            "status":               status,
            "order_date":           order_date,
            "paid_date":            paid_date,
            "customer_id":          customer_id,
            "customer_email":       bill_email,
            "payment_method":       payment,
            "payment_method_title": payment.replace("_", " ").title(),
            "transaction_id":       safe(row, "transaction_id", "TransactionID"),
            "order_currency":       currency,
            "billing_first_name":   bill_first,
            "billing_last_name":    bill_last,
            "billing_company":      bill_company,
            "billing_address_1":    bill_addr1,
            "billing_address_2":    bill_addr2,
            "billing_city":         bill_city,
            "billing_state":        bill_state,
            "billing_postcode":     bill_postcode,
            "billing_country":      bill_country,
            "billing_email":        bill_email,
            "billing_phone":        bill_phone,
            "shipping_first_name":  ship_first,
            "shipping_last_name":   ship_last,
            "shipping_company":     ship_company,
            "shipping_address_1":   ship_addr1,
            "shipping_address_2":   ship_addr2,
            "shipping_city":        ship_city,
            "shipping_state":       ship_state,
            "shipping_postcode":    ship_postcode,
            "shipping_country":     ship_country,
            "shipping_phone":       ship_phone,
            "order_subtotal":       order_subtotal,
            "shipping_total":       shipping,
            "tax_total":            tax,
            "order_total":          total,
            "line_items":           line_items_str,
            "shipping_lines":       shipping_lines_str,
            "coupon_code":          coupon,
            "customer_note":        cust_note,
            "order_notes":          order_note,
        })
        out_rows.append(out)

    return out_rows


# ═════════════════════════════════════════════════════════════
# FILE I/O HELPERS
# ═════════════════════════════════════════════════════════════

def read_csv(path: str) -> tuple[list[str], list[dict]]:
    """Read a CSV file and return (headers, rows)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)
    return list(headers), rows


def write_csv(path: str, columns: list[str], rows: list[dict]) -> None:
    """Write rows to a CSV file using the given column order."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def find_csv_files(folder: str) -> list[str]:
    """Return all CSV files in a folder (non-recursive)."""
    return sorted(
        str(p) for p in Path(folder).glob("*.csv") if p.is_file()
    )


def process_folder(input_dir: str, output_dir: str, inspect: bool = False) -> None:
    """
    Detect and convert every CSV in a folder. Addresses are matched to
    customers by id_customer if both an addresses file and a customers
    file are found (PrestaShop keeps them separate).
    """
    csv_files = find_csv_files(input_dir)
    if not csv_files:
        print(f"  ✗ No CSV files found in: {os.path.abspath(input_dir)}")
        sys.exit(1)

    print(f"  Found {len(csv_files)} CSV file(s) in: {os.path.abspath(input_dir)}")

    # ── First pass: read + detect every file ────────────────
    detected: list[tuple[str, str, list[str], list[dict]]] = []
    for path in csv_files:
        print(f"\n  ▸ Reading: {path}")
        headers, rows = read_csv(path)
        if not rows:
            print("    ✗ File is empty — skipping.")
            continue
        if inspect:
            print(f"    ℹ  {len(rows)} rows  |  {len(headers)} columns:")
            for i, h in enumerate(headers, 1):
                sample = str(rows[0].get(h, "")).strip()[:40]
                print(f"       {i:>3}. {h!r:<40}  sample: {sample!r}")
            print()
        file_type = detect_file_type(headers)
        print(f"    ✓ Detected type: {file_type.upper()}")
        detected.append((path, file_type, headers, rows))

    # ── Build an address lookup if an addresses file is present ──
    addresses_by_customer: dict[str, dict] = {}
    for path, file_type, headers, rows in detected:
        if file_type == "addresses":
            addresses_by_customer = index_addresses(rows)
            print(f"\n  ℹ  Linked {len(addresses_by_customer)} address(es) "
                  f"to customers from: {path}")

    # ── Second pass: convert ────────────────────────────────
    for path, file_type, headers, rows in detected:
        stem = Path(path).stem

        if file_type == "products":
            converted = convert_products(rows)
            out_path  = os.path.join(output_dir, "woocommerce_products.csv")
            write_csv(out_path, WOO_PRODUCT_COLS, converted)

        elif file_type == "orders":
            converted = convert_orders(rows)
            out_path  = os.path.join(output_dir, "woocommerce_orders.csv")
            write_csv(out_path, WOO_ORDER_COLS, converted)

        elif file_type == "customers":
            converted = convert_customers(rows, addresses_by_customer)
            out_path  = os.path.join(output_dir, "woocommerce_customers.csv")
            write_csv(out_path, WOO_CUSTOMER_COLS, converted)

        elif file_type == "addresses":
            # Already consumed above to enrich customers — nothing to write.
            print(f"\n  ▸ Skipping standalone write for: {path} (addresses merged into customers)")
            continue

        else:
            print(f"\n  ✗ Could not detect file type for: {path}")
            print(f"    Headers found: {headers[:8]} ...")
            print("    Run with --inspect to see all column names.")
            print("    Skipping this file.")
            continue

        print(f"    ✓ Converted {len(rows)} rows  →  {len(converted)} output rows")
        print(f"    ✓ Saved: {out_path}")


def process_file(input_path: str, output_dir: str, inspect: bool = False) -> None:
    """Auto-detect type and convert a single PrestaShop CSV (no address merge)."""
    print(f"\n  ▸ Reading: {input_path}")
    headers, rows = read_csv(input_path)

    if not rows:
        print("    ✗ File is empty — skipping.")
        return

    if inspect:
        print(f"    ℹ  {len(rows)} rows  |  {len(headers)} columns:")
        for i, h in enumerate(headers, 1):
            sample = str(rows[0].get(h, "")).strip()[:40]
            print(f"       {i:>3}. {h!r:<40}  sample: {sample!r}")
        print()

    file_type = detect_file_type(headers)
    print(f"    ✓ Detected type: {file_type.upper()}")

    if file_type == "products":
        converted = convert_products(rows)
        out_path  = os.path.join(output_dir, "woocommerce_products.csv")
        write_csv(out_path, WOO_PRODUCT_COLS, converted)

    elif file_type == "orders":
        converted = convert_orders(rows)
        out_path  = os.path.join(output_dir, "woocommerce_orders.csv")
        write_csv(out_path, WOO_ORDER_COLS, converted)

    elif file_type == "customers":
        converted = convert_customers(rows)
        out_path  = os.path.join(output_dir, "woocommerce_customers.csv")
        write_csv(out_path, WOO_CUSTOMER_COLS, converted)

    elif file_type == "addresses":
        print("    ℹ  This is a standalone PrestaShop addresses export.")
        print("       Run the whole input folder (without --file) together")
        print("       with your customers.csv so addresses get merged in.")
        return

    else:
        print(f"    ✗ Could not detect file type for: {input_path}")
        print(f"      Headers found: {headers[:8]} ...")
        print("      Run with --inspect to see all column names.")
        print("      Skipping this file.")
        return

    print(f"    ✓ Converted {len(rows)} rows  →  {len(converted)} output rows")
    print(f"    ✓ Saved: {out_path}")


# ═════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═════════════════════════════════════════════════════════════

BANNER = """
╔══════════════════════════════════════════════════════════════╗
║       PrestaShop → WooCommerce CSV Migration Tool  v1.0     ║
╚══════════════════════════════════════════════════════════════╝
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert PrestaShop CSV exports to WooCommerce import CSVs."
    )
    parser.add_argument("--input",   "-i", default="input",
                        help="Folder containing PrestaShop CSV files (default: ./input)")
    parser.add_argument("--output",  "-o", default="output",
                        help="Folder for WooCommerce output CSVs (default: ./output)")
    parser.add_argument("--file",    "-f", default=None,
                        help="Process a single CSV file instead of the entire input folder.")
    parser.add_argument("--inspect", "-d", action="store_true",
                        help="Print all column names + sample values (debug mode).")
    args = parser.parse_args()

    print(BANNER)

    output_dir = args.output
    os.makedirs(output_dir, exist_ok=True)
    print(f"  Output folder: {os.path.abspath(output_dir)}\n")

    if args.file:
        if not os.path.isfile(args.file):
            print(f"  ✗ File not found: {args.file}")
            sys.exit(1)
        process_file(args.file, output_dir, inspect=args.inspect)
        print("\n  ✔ Done.\n")
        return

    input_dir = args.input
    if not os.path.isdir(input_dir):
        print(f"  Input folder '{input_dir}' not found.")
        user_input = input(
            "  Enter the path to your input folder (or press Enter to exit): "
        ).strip()
        if not user_input:
            print("  Exiting.")
            sys.exit(0)
        input_dir = user_input

    if not os.path.isdir(input_dir):
        print(f"  ✗ Folder not found: {input_dir}")
        sys.exit(1)

    process_folder(input_dir, output_dir, inspect=args.inspect)

    print("\n  ══════════════════════════════════════════════════")
    print(f"  ✔ All files processed.  Output → {os.path.abspath(output_dir)}")
    print("  ══════════════════════════════════════════════════\n")

    print("  WooCommerce Import Guide:")
    print("  ─────────────────────────")
    print("  Products  → WooCommerce > Products > Import (built-in importer)")
    print("  Customers → WebToffee 'WordPress Users & WooCommerce Customers Import Export'")
    print("  Orders    → WebToffee 'Order Import Export for WooCommerce'")
    print("  Addresses → merged automatically into customers.csv output if an")
    print("              addresses export is present in the same input folder")
    print()


if __name__ == "__main__":
    main()