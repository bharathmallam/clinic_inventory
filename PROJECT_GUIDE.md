# Ultrahealth Clinic Inventory: Project Guide

> **The single source of truth for this project.** Reading this document end to end should tell you everything: what the system does, who can do what, how the database is built, how the code is laid out, how to run, test and deploy it, and what is still open.
> It is updated with every change. See the [Change log](#15-change-log). Open work lives in [TODO.md](TODO.md), and data to clean up in [DATA_ISSUES.md](DATA_ISSUES.md).

**Last updated:** 2026-10-12 · **Version:** 1.0.8

---

## Contents
1. [What this system is](#1-what-this-system-is)
2. [Access: levels and categories](#2-access-levels-and-categories)
3. [How a request moves](#3-how-a-request-moves)
4. [Business rules](#4-business-rules)
5. [Stock: single source of truth](#5-stock-single-source-of-truth)
6. [Database design](#6-database-design)
7. [Data safety: ACID and integrity](#7-data-safety-acid-and-integrity)
8. [Technology and hosting](#8-technology-and-hosting)
9. [Project structure](#9-project-structure)
10. [API reference](#10-api-reference)
11. [Screens](#11-screens)
12. [Running and testing locally](#12-running-and-testing-locally)
13. [Deploying and going live](#13-deploying-and-going-live)
14. [Branding](#14-branding)
15. [Change log](#15-change-log)

---

## 1. What this system is

A small inventory and request-approval system for the **Ultrahealth** clinic (by Dr. Pooja Reddy).

Stock is kept in **categories**, which are data, not code: Clinic Consumables, Pantry and Housekeeping to begin with, and any others the clinic adds later. A new category brings its own products and its own people, with no change to the software.

Someone **raises a request** for what they need, a **checker approves or rejects** each product line, the **store hands it over**, and the person **confirms they received it**. Stock only ever moves through a ledger, and every action is recorded in an audit log that cannot be edited.

Goods come in on **purchase bills** entered against a fixed **vendor list** (each vendor keyed by a checked GSTIN). Bills are copied as printed — so many packs, so many units in a pack — while stock is always counted in the unit the clinic issues in.

---

## 2. Access: levels and categories

There are **four access levels**. They never change. What does change is the list of categories, so a person is given a level **per category**.

| Level | Can | Cannot |
|---|---|---|
| **Maker** | Raise requests in their categories; see those products with the available quantity only; confirm receipt of their own orders; cancel their own request while it is still waiting | Approve, deliver, or see stock levels, costs or prices |
| **Checker** | Approve or reject requests in their categories (never their own); add and edit those products; see their stock and reports | Deliver; change a quantity at approval |
| **Store** | Deliver approved requests, add and adjust stock, enter purchase bills, vendors and payments, manage products | Approve requests |
| **Administrator** | Accounts and their access, categories, the clinic's own details, the audit log | Request, approve, deliver, or touch stock |

**How access is given.** One row per person, per level, per category (`staff_access`). So Priya can be a maker for Pantry only, while Ravi is a maker for Pantry and a checker for Housekeeping. Store and Administrator rows carry no category: they cover every category, including ones added later.

**Adding a category** (Lists → Categories, Administrator only) takes effect at once: it appears on the Users screen, products can be added to it, and nobody can act in it until the Administrator ticks who may. Each category also says whether its stock **expires** and whether its bills need an **HSN code**.

**Removing one.** A category that nothing has used — no products, nobody given access — shows a **Delete** button and goes for good, so a mistyped name need not sit in the list forever. The moment anything uses it, Delete disappears and the only route is **Switch off**: it vanishes from every tab, drop-down and the Users grid, while its products, bills and history stay readable. Switching off is refused while the category still has active products. Categories are not edited after they are created; if one is wrong, delete it while it is empty and add it again.

**Permissions are data too.** Each level's permissions are rows in `level_permissions` (`request.create`, `stock.manage`, `audit.view` …), so what a level may do is visible in the database rather than buried in code.

**Keep at least two checkers per category.** A checker's own request needs a second checker.

---

## 3. How a request moves

```mermaid
flowchart LR
    S([Request sent<br/>1..n products]) --> A[request_items<br/>PENDING, per product]
    A -- Checker approves<br/>(exactly as asked) --> B[APPROVED]
    B -- Store hands over<br/>stock reduced --> C[DELIVERED]
    A -- Checker rejects<br/>(reason required) --> R[REJECTED]
    A -- requester cancels --> X[CANCELLED]
    C --> O([Order code<br/>+ receipt confirmed])
```

- **One request, several products.** On New request people type quantities straight onto the product rows — across pages, searches and categories — and press Request. A pop-up lists exactly what is about to be ordered; OK sends it. The database keeps the full number `REQ-20261005-7F3A2C`; **screens show only the short code `7F3A2C`**, which is unique on its own, and each product is a line of it (`7F3A2C-1`, `7F3A2C-2`, …). Hovering the code shows the full number.
- **Nobody can ask for more than is available.** The quantity box refuses to go past the available figure as it is typed — it stops at that number and says so beside the box — and the server checks again when the request is sent.
- **A line is approved in full or rejected.** Quantities are never edited at approval. If stock is short, the checker rejects with a reason — "only 6 available, please ask for 6" — and the person raises a fresh request.
- **A request may end partly fulfilled**: some lines delivered, others rejected. An individual line never is.
- **When a request finishes** (nothing left waiting) and at least one line was handed over, the app creates an **order**: a short code such as `AF470D`, unique on its own, which people quote. The database also keeps the full `ORD-20261005-AF470D`, **which is never put on screen or in an export** — not in a column, a tile, a tooltip or a spreadsheet. The same holds for `REQ-20261005-7F3A2C`. Searching the audit log by the short code finds the request.
- **The order lists every line of the request with what happened to it** — Delivered, Rejected (with the reason and who decided) or Cancelled — so the person who asked can see that an item was turned down by someone else rather than quietly lost. The Orders list says "2 products, 1 not given".
- **The person confirms receipt** on the order. Only they can, only once, and it is stamped with their name and time — so "delivered" and "received" are two separate statements. The confirmation covers the delivered lines only.
- **Where each line lives on screen:** waiting → My requests; handed over → Orders (Delivered orders); rejected or cancelled → Orders (Rejected & cancelled).

---

## 4. Business rules

| # | Rule | App | Database |
|---|---|---|---|
| 1 | Nobody decides their own request | ✓ | ✓ CHECK `decided_by <> requested_by` |
| 2 | Only a checker of that category decides a line | ✓ | |
| 3 | **Quantities are never changed at approval**: approve as asked, or reject | ✓ | |
| 4 | **Rejecting needs a reason** ("Please mention the reason and the available quantity") | ✓ | ✓ CHECK |
| 5 | A request may be partly fulfilled; a line may not | ✓ | |
| 6 | **Nobody may request more than is available** (usable stock − already approved): the box stops at that number while typing, and the server re-checks on sending | ✓ | |
| 7 | A product at 0 available is shown, greyed, and cannot be ordered | ✓ | |
| 8 | The store delivers exactly the approved quantity, only for approved lines; stock never goes below 0 | ✓ | ✓ CHECK `current_stock >= 0` |
| 9 | Only the person who raised a request confirms receipt, once | ✓ | ✓ composite FK + unique |
| 10 | Makers see only their own requests and orders; **nothing a maker receives contains stock figures, costs or prices** — only a product's *available* quantity | ✓ (tested) | |
| 11 | Nobody changes their own access or switches off their own account | ✓ | |
| 12 | Maker and checker access always names a category; store and admin cover all | ✓ | ✓ consistency check |
| 13 | **Stock only changes through the stock ledger** (section 5) | ✓ | ✓ triggers |
| 14 | History can't be edited or deleted: audit log, stock ledger, request-line history, orders, acknowledgements | ✓ | ✓ triggers |
| 15 | New accounts and reset passwords are temporary: the person must choose their own at first sign-in | ✓ | |
| 16 | A batch's expiry is fixed: the same batch number can't come in again with a different expiry; expired stock is never delivered and never counts as available | ✓ | ✓ unique (product, batch no) |
| 17 | Vendors, units, HSN codes and categories are **picked from lists**; only active entries can be picked | ✓ | ✓ foreign keys |
| 18 | A vendor's **GSTIN** passes format and check-digit tests and is unique; state and PAN are read from it | ✓ | ✓ unique |
| 19 | One bill number per vendor; bill totals are **calculated from the lines** (only the round-off, ±₹10, is typed); a posted bill can only be cancelled, and only while its stock is untouched | ✓ | ✓ unique + CHECK |
| 20 | GST: same state as the clinic → CGST + SGST, other state → IGST, composition vendor → none. The rate comes from the HSN list and is copied onto the line | ✓ | ✓ CHECK rate 0–40 |
| 21 | A bill may be paid in several payments; payments never exceed the bill | ✓ | ✓ CHECK amount > 0 |
| 22 | Machines and services go on a bill as **non-stock lines** | ✓ | ✓ CHECK |
| 23 | **Stock is counted in the unit it is issued in.** A bill records packs × units per pack; what enters stock is the issue unit | ✓ | ✓ CHECK |

---

## 5. Stock: single source of truth

The **stock ledger** (`stock_movements`) is the truth. `products.current_stock` is its running total, kept for speed.

| Event | Ledger row | Stock |
|---|---|---|
| New product with opening stock | `RESTOCK +n` ("Opening stock") into a batch | +n |
| **Purchase bill posted** | `RESTOCK +n` per stock line (packs × units per pack, free included), linked to the bill line | +n |
| **Purchase bill cancelled** | `ADJUSTMENT −n` per line, from the same batch | −n |
| **Stock → Restock** (batch no + expiry) | `RESTOCK +n` | +n |
| **Stock → Adjustment** (damaged, count correction; note required) | `ADJUSTMENT ±n` | ±n |
| Store marks a line **delivered** | `DELIVERY −n` per batch used, **earliest expiry first** | −approved qty |
| Checker **approves** | none (stock is only *promised*; see "available") | unchanged |

Enforced by the **database itself**, not only the app:
- A trigger (`trg_products_stock_via_ledger`) refuses any change to `products.current_stock` unless the newest ledger row for that product carries exactly that change and new balance. The app writes the ledger row first, then the stock.
- A trigger (`trg_products_start_at_zero`) makes every new product start at 0 (opening stock is a ledger row).
- Ledger rows can't be edited or deleted.
- `python manage.py check` confirms every product's stock equals its ledger total, and that the batches add up to it.

**Batches.** Every ledger row belongs to a batch (`batches`: batch number + expiry). A batch's quantity is **not stored**: it is the sum of its ledger rows. Deliveries take from the batch with the earliest expiry (no-expiry batches last) and skip expired ones. **A batch number that isn't known is left empty** and shown as "—"; typing `-`, `NA` or `NIL` means the same. A batch's cost is the average of the bill lines that brought it in, which gives the **stock value** report, so prices are never typed onto products.

**Available** = usable stock (not expired) − quantities approved but not yet handed over.

---

## 6. Database design

MySQL 8 in production (on the AWS Lightsail server), **MySQL 8.0 locally**, SQLite for quick tests. The readable DDL is [database/schema.sql](database/schema.sql). It is **generated** from [backend/app/models.py](backend/app/models.py), so don't edit it by hand. A drawing of every table and key is on the Desktop as `Ultrahealth-Database-Map.html`.

### 6.1 Diagram

```mermaid
erDiagram
    access_levels ||--o{ level_permissions : "may do"
    access_levels ||--o{ staff_access : ""
    staff ||--o{ staff_access : "is"
    categories ||--o{ staff_access : "in"
    categories ||--o{ products : "contains"
    units ||--o{ products : "counted in"
    hsn_codes ||--o{ products : "tax class"
    vendors ||--o{ vendor_contacts : "people"
    staff ||--o{ requests : "raises"
    requests ||--|{ request_items : "lines"
    products ||--o{ request_items : "requested"
    request_items ||--o{ request_item_history : "each step"
    requests ||--o| orders : "one when finished"
    orders ||--o| order_acknowledgements : "receipt"
    products ||--o{ batches : "batches"
    batches ||--o{ stock_movements : "ledger"
    request_items ||--o{ stock_movements : "delivery"
    vendors ||--o{ invoices : "bills"
    invoices ||--|{ invoice_items : "lines"
    invoices ||--o{ payments : "paid by"
    invoice_items ||--o{ stock_movements : "received"
    staff ||--o{ audit_log : "acts"
```

### 6.2 Tables

Every table has a **primary key**, uses InnoDB (transactions + foreign keys) and utf8mb4. Timestamps are UTC. A table people edit carries `created_by/created_dt` **and** `updated_by/updated_dt`; a write-once table carries only `created_by/created_dt`, because the database refuses updates to it.

| Table | Primary key | What it holds | Key columns |
|---|---|---|---|
| `organisation` | `id` (always 1) | The clinic itself | `name`, `gstin`, `state_code` |
| `categories` | `id` | The parts of the clinic's stock; add any time | `code`, `name`, `tracks_expiry`, `needs_hsn`, `is_active` |
| `units` | `id` | Units of issue — a fresh database has none; the clinic adds its own | `code`, `name`, `is_active` |
| `hsn_codes` | `id` | HSN codes with today's GST rate | `code`, `description`, `gst_rate` |
| `vendors` | `id` | Who the clinic buys from | `name`, `gstin` (unique, verified), `is_composition`, `is_active` |
| `vendor_contacts` | `id` | People to speak to at a vendor | `vendor_id`, `name`, `role`, `phone` |
| `products` | `id` | One row per product, every category | `sku`, `name`, `category_id`, `unit_id`, `hsn_id`, `pack_size`, `pack_label`, `reorder_level`, `current_stock` |
| `access_levels` | `id` | MAKER / CHECKER / STORE / ADMIN | `code`, `needs_category` |
| `level_permissions` | `id` | What each level may do | `level_id`, `permission` |
| `staff` | `id` | Accounts | `username`, `full_name`, `email`, `password_hash`, `must_change_password` |
| `staff_access` | `id` | Who is what, where | `staff_id`, `level_id`, `category_id` (empty = all) |
| `requests` | `id` | A request as sent | `request_no`, `requested_by`, `purpose`, `requested_dt` |
| `request_items` | `id` | One product line and its stage | `request_id`, `line_no`, `product_id`, `quantity`, `status`, `decided_by`, `reason`, `delivered_by` |
| `request_item_history` | `id` | Every step a line passed through (write-once) | `request_item_id`, `status`, `created_by`, `note` |
| `orders` | `id` | One order per completed request (write-once) | `order_no`, `short_code` (unique), `request_id`, `completed_dt` |
| `order_acknowledgements` | `id` | Receipt confirmed by the requester (write-once) | `order_id` (unique), `acknowledged_by`, `note` |
| `batches` | `id` | A batch of one product | `product_id`, `batch_no`, `expiry_date`, `received_dt` |
| `stock_movements` | `id` (BIGINT) | The stock ledger (write-once) | `product_id`, `batch_id`, `quantity_change`, `balance_after`, `reason`, `request_item_id`, `invoice_item_id` |
| `invoices` | `id` | A vendor's bill | `vendor_id` + `invoice_no` (unique together), `invoice_date`, `round_off`, `status` |
| `invoice_items` | `id` | One bill line | **`invoice_id` → `invoices.id`** (ON DELETE CASCADE, and `invoice_id` + `line_no` unique), `line_type`, `product_id` or `description`, `hsn_id`, `gst_rate`, `batch_no`, `expiry_date`, `pack_quantity`, `units_per_pack`, `free_packs`, `rate`, `discount_percent`, `mrp` |
| `payments` | `id` | Money paid against a bill | `invoice_id`, `amount`, `paid_on`, `method`, `reference` |
| `audit_log` | `id` (BIGINT) | Every action (write-once) | `action`, `entity`, `actor_id`, `actor_levels`, `request_no`, `details` (JSON) |
| `alembic_version` | `version_num` | Which schema version the database is on | |

**Where each fact lives (single source of truth):** a vendor's details → `vendors`; its state and PAN → read from the GSTIN; a GST rate → `hsn_codes`, copied onto a bill line so old bills never change; a unit name → `units`; stock → the ledger; a batch's quantity → the sum of its ledger rows; a bill's totals and payment state → its lines and payments; a request's overall state → its lines; a product's cost → the bills that brought its batches in.

### 6.3 Design decisions
- **Categories are data.** No segment is named in code, so "Lab Consumables" needs no release.
- **Access is a three-way row** (person × level × category) rather than a role column, so one person can be a maker here and a checker there.
- **One line, one row.** A request line keeps its status rather than moving between tables; its history is a separate write-once table, so "who moved it and when" survives.
- **Nothing derived is stored.** Bill totals, batch quantities, payment status and a request's state are worked out when read. The single exception is `products.current_stock`, which a trigger ties to the ledger.
- **Packs are a data-entry aid, not a second unit.** A bill line records packs and units per pack; stock, requests, deliveries and reports only ever speak the issue unit.
- **No hard deletes** of people, products, vendors or categories: they are switched off.
- **Naming**: tables snake_case plural; constraints `pk_`, `fk_`, `uq_`, `ck_`, `ix_` + table + column.

---

## 7. Data safety: ACID and integrity

| Property | How |
|---|---|
| **Atomicity** | Every action (e.g. "Deliver all" = status changes + ledger rows + stock + history + audit rows) runs inside one `atomic()` transaction: all saved, or on any error nothing. Tested. |
| **Consistency** | Primary, foreign (incl. composite), unique, NOT NULL and CHECK constraints for the rules in section 4; triggers for section 5; MySQL **strict mode**; `python manage.py check` (stock = ledger, batches add up, decisions recorded, every delivered line moved stock, every account has access). |
| **Isolation** | InnoDB `REPEATABLE READ` + row locks (`SELECT … FOR UPDATE`) on the line and the product. No double approvals, no overbooking, no overselling. Locks are always taken in the same order: request line → product. |
| **Durability** | InnoDB redo log; `manage.py backup` daily by a systemd timer (consistent snapshot, compressed, private) and Lightsail's automatic snapshots of the whole server. Restoring was rehearsed. |
| **Schema changes** | Alembic migrations in `backend/migrations/`. A test proves migrations == models on both databases. |

**Nothing floats loose.** All 22 tables have a primary key, and every column that names another row is a real foreign key — 72 of them, four of which are composite so the database itself can check a rule (`request_items(request_id, requested_by) → requests(id, requested_by)` is what lets `CHECK decided_by <> requested_by` mean "nobody approves their own request"). A parent and its lines are tied the same way everywhere: `invoice_items.invoice_id → invoices.id`, `request_items.request_id → requests.id`, `payments.invoice_id → invoices.id`, `batches.product_id → products.id`, `stock_movements` back to its batch, its request line and its bill line. Where a repeat would be wrong there is a unique key: `(invoice_id, line_no)`, `(request_id, line_no)`, `(vendor_id, invoice_no)`, `(category_id, name)`, `(product_id, batch_no)`, `(staff_id, level_id, category_id)`, and the codes `sku`, `short_code`, `gstin`, `username`.

The **one** column that ends in `_id` and is deliberately not a foreign key is `audit_log.entity_id`: one log row may describe a product, a bill or a category, so it is stored as text, and it has to keep reading correctly after the thing it describes is gone. `organisation` has no unique key either — a CHECK constraint holds it to a single row, the clinic itself.

---

## 8. Technology and hosting

| Part | Choice | Why |
|---|---|---|
| Frontend | React 18 + Vite | Builds to static files nginx serves directly |
| Backend | Python **3.10+** + Flask (WSGI), run by **Gunicorn** under systemd | Plain WSGI: no host-specific glue |
| Hosting | **AWS Lightsail**: one Ubuntu 24.04 instance with nginx (website + `/api` proxy), Gunicorn, MySQL 8 and Let's Encrypt HTTPS | Predictable monthly price; the database never faces the internet |
| Database | MySQL 8 (live and local), SQLite (fast tests) | Same engine locally as live |
| ORM / migrations | SQLAlchemy 2 + Flask-Migrate (Alembic) | One model definition drives tables, migrations, schema.sql |
| Auth | Username or email + password (salted hash, 8+ chars), JWT kept **per browser tab** (closing the tab signs out); a sign-in **ends 24 hours** after signing in and the screen signs itself out | Works on shared hosting |
| Versions | All Python packages pinned in `requirements.txt` | Same code everywhere |

---

## 9. Project structure

```
Inventory/
├── PROJECT_GUIDE.md          ← this document
├── TODO.md · DATA_ISSUES.md · README.md · CLAUDE.md · VERSION
├── start.sh                  one command: local MySQL + backend + frontend (LAN=1 for other devices)
├── scripts/                  local-mysql.sh · build-release.sh · build-docs.py · check-live-site.sh
├── DEPLOY.md                 step-by-step AWS Lightsail runbook (ships inside the package)
├── deploy/lightsail/         install.sh (install + update), nginx site, systemd app + backup timer, MySQL settings
├── database/schema.sql       generated MySQL DDL (reference)
├── examples/                 the clinic's workbooks + prepared/ import files
├── backend/
│   ├── wsgi.py               entry point (Gunicorn on the server, flask --app wsgi locally)
│   ├── manage.py             deploy, preflight, backup, imports/exports, setup commands
│   ├── migrations/           schema history
│   ├── tests/                access, requests, stock, purchases, deployment (54 tests)
│   └── app/
│       ├── models.py         all tables, constraints, relationships
│       ├── gst.py            GSTIN check digit, state codes, GST slabs
│       ├── auth.py           JWT, permission check, forced password change
│       ├── routes/           auth · products · requests · orders · purchasing · admin
│       └── services/
│           ├── core.py       errors, atomic(), parsers
│           ├── stock.py      the ledger, batches, FEFO, availability
│           ├── requests.py   raise, approve, reject, cancel, deliver
│           ├── orders.py     order codes and acknowledgement
│           ├── catalogue.py  products
│           ├── masters.py    categories, units, HSN, vendors, the clinic
│           ├── accounts.py   staff and their access
│           ├── purchases.py  bills, posting, cancelling, payments
│           ├── reports.py    monthly usage, stock value
│           ├── transfer.py   spreadsheet import/export
│           ├── integrity.py  triggers + consistency check
│           ├── ops.py        pre-flight checks, backups
│           └── audit.py      the only writer to audit_log
└── frontend/
    ├── public/ (templates/ CSV templates, brand/, fonts/)
    └── src/pages/            Login, ChangePassword, Inventory, NewRequest, MyRequests, Approvals,
                              Deliveries, Orders, RequestDetail, Bills, Vendors, Lists, Users,
                              AuditLog, Reports, ImportPanel
```

---

## 10. API reference

All under `/api`, JSON, and (except login/health) `Authorization: Bearer <token>`.

| Method & path | Who | Purpose |
|---|---|---|
| `POST /auth/login` | anyone | `username` (or email) + `password` → token + user |
| `GET /auth/me` · `POST /auth/change-password` | signed in | Current user · set own password |
| `GET /products/search?q=&category_id=&page=` | request.create | What the caller may order, 10 a page, with **available** only |
| `GET /products?category_id=&q=` | inventory.view | Inventory with batches, reserved and available |
| `POST /products` · `PATCH /products/<id>` | product.manage | Add / edit / retire a product |
| `POST /products/<id>/stock` | stock.manage | Restock (batch no + expiry) or adjust (optionally one batch) |
| `GET /products/<id>/movements` · `GET /products/export` · `POST /products/import` | inventory.view / product.manage | Ledger · spreadsheet out and in |
| `POST /requests` | request.create | `{"items": [{"product_id","quantity"}…], "purpose"}` |
| `GET /requests/mine` · `/pending` · `/approved` · `/closed` · `/<request_no>` | signed in (makers: own only) | Lines by stage · one request with its history and audit trail |
| `POST /requests/<line>/approve` · `/reject` · `/cancel` · `/deliver` | checker · requester · store | One line at a time (`REQ-…-2`); rejecting needs a reason |
| `POST /requests/<request_no>/approve-all` · `/deliver-all` | checker · store | Whole request, all-or-nothing |
| `GET /orders?q=&category_id=&month=` · `GET /orders/<code>` | signed in (makers: own only) | Completed handovers, by short code |
| `POST /orders/<code>/acknowledge` | the requester | Confirm receipt, once |
| `GET /orders/export` | inventory.view | Orders as a spreadsheet |
| `GET/POST /categories` · `PATCH`/`DELETE /categories/<id>` | signed in · category.manage | The categories list; DELETE only while unused |
| `GET/POST /vendors` · `PATCH /vendors/<id>` · contacts · `/vendors/import` | inventory.view · purchase.manage | Vendors, their people, bulk import |
| `GET /gstin/<gstin>` | inventory.view | Live check: valid?, state, PAN, already used by |
| `GET/POST /units` · `/hsn` (+ `PATCH`, `/hsn/import`) | signed in · product.manage | The small lists |
| `GET /organisation` · `PATCH /organisation` | signed in · user.manage | The clinic's name and GSTIN |
| `GET /bills?…` · `GET /bills/<id>` | inventory.view | Bills with calculated totals and a summary |
| `POST /bills` · `PUT /bills/<id>` (`"post": true`) · `DELETE` · `/post` · `/cancel` | purchase.manage | Draft, post (stock in), cancel (stock out) |
| `POST /bills/<id>/payments` · `DELETE /bills/payments/<id>` | purchase.manage | Record or remove a payment |
| `GET /reports/usage?month=&category_id=&format=csv` · `GET /reports/stock-value` | reports.view | Monthly usage · value of stock at cost |
| `GET/POST /staff` · `PATCH /staff/<id>` · `/reset-password` · `/staff/import` | user.manage | Accounts and access |
| `GET /levels` | signed in | The four levels and their permissions |
| `GET /audit?action=&request_no=&…` · `GET /audit/actions` | audit.view | Audit log |
| `GET /health` | anyone | `{"status":"ok","version":…}` |

---

## 11. Screens

**Errors and warnings are shown as a pop-up over the page**, not as a strip at the top, so they are seen wherever the person has scrolled to. A success stays a quiet line at the top.

The menu shows only what the person's access allows. It scrolls separately from the page; **☰** hides it on a computer and opens it as a drawer on a phone. The brand in the corner is a link home.

**Every page that can be opened on its own carries a ← back control** at the top left — the order page, a request, a bill, the bill form, the password screen, and the "not found" version of each. It steps back to where the person came from; opened cold from a bookmark or a pasted link, it goes to the list that page belongs to. Pop-ups close with **Esc**, with the Cancel button or by clicking outside, keeping whatever was typed; a new page opens at its top; and the browser tab is named after the page ("Order 3DB348 · Ultrahealth Inventory").

| Screen | Maker | Checker | Store | Admin |
|---|---|---|---|---|
| Inventory (category tabs; filters for low stock, expiring, expired; batches with expiry, cost and MRP; Stock, Edit, New product, Import, Export) | – | own categories | ✓ all | view |
| New request (one tab per category; 10 products a page; available quantity; type quantities, press Request, confirm in a pop-up) | ✓ | ✓ | ✓ | – |
| My requests (only what is still waiting; cancel your own) | ✓ | ✓ | ✓ | – |
| Approvals (grouped by request; **Approve as asked** or **Reject with a reason**; Approve all) | – | ✓ own categories | – | – |
| Deliveries (grouped; Mark delivered / Deliver all; stock warnings) | – | – | ✓ | – |
| Orders — finished work, two tabs: **Delivered orders** (short code, who ordered, when, receipt; the code opens the order) and **Rejected & cancelled** | own | ✓ | ✓ | ✓ |
| Order page (every line of the request with its status — delivered, rejected with reason, cancelled — who approved and delivered, and the receipt) | own | ✓ | ✓ | ✓ |
| Purchase bills (list with totals and what is still to pay; bill form with packs, live GST maths and a printed-total check; post, cancel, record payments) | – | view | ✓ | view |
| Vendors (live GSTIN check, composition scheme, contacts, import) | – | view | ✓ | view |
| Reports (monthly usage matrix + CSV; stock value) | – | ✓ | ✓ | ✓ |
| Lists (categories and units — add, delete while unused, switch off; HSN codes; the clinic's GSTIN) | – | units/HSN | units/HSN | ✓ categories + clinic |
| Users (add people, tick their level per category, reset passwords, import) | – | – | – | ✓ |
| Audit log | – | – | – | ✓ |
| Password (change own; forced at first sign-in with a temporary password) | ✓ | ✓ | ✓ | ✓ |

---

## 12. Running and testing locally

```bash
./start.sh        # then open http://localhost:5173 ; Ctrl+C stops everything
```

**Opening it from another device (no GitHub needed):**
- Same Wi-Fi: `LAN=1 ./start.sh`, then open the "Same Wi-Fi" address it prints (e.g. `http://192.168.0.3:5173`). macOS may ask to allow incoming connections: click Allow.
- From anywhere: keep `./start.sh` running and, in a second terminal, `ngrok http 5173`. **The test logins work for anyone with the link**, so stop ngrok when done.

`start.sh` starts the local MySQL, applies migrations, seeds the access levels, categories and units, and on a fresh database adds sample products and the **test logins** (password `test` for all):

| Username | Access |
|---|---|
| `admin` | Administrator |
| `manager`, `manager2` | Checker in every category (two, so each can approve the other's requests) |
| `store` | Store |
| `staff` | Maker in every category |

These only exist when `APP_ENV=development`; the live site refuses them, and pre-flight fails if a weak password exists there.

**Tests** (inside `backend/`):

| Command | Runs |
|---|---|
| `.venv/bin/python -m pytest` | all tests on SQLite (~3 min) |
| `TEST_DATABASE_URL="mysql+pymysql://inventory:PASSWORD@127.0.0.1:3306/inventory_test?charset=utf8mb4" .venv/bin/python -m pytest` | the same on MySQL |

Other commands: `manage.py seed-dev-users`, `bootstrap-dev`, `check`, `export-schema`, `set-clinic`, and after changing models `flask --app wsgi:application db migrate -m "…"`.

**The two reading pages** on the Desktop — *Ultrahealth-Project-Guide.html* (this document) and *Ultrahealth-Database-Map.html* (every table and link) — are rebuilt by `python3 scripts/build-docs.py` from `PROJECT_GUIDE.md` and `database/schema.sql`. It also refreshes `frontend/public/guide.html` and `schema.html`. Run it after changing either source; run `manage.py export-schema` first if the models changed.

---

## 13. Deploying and going live

Everything is in **[DEPLOY.md](DEPLOY.md)**, which also ships inside the package. In short:

1. On your Mac: `scripts/build-release.sh` → runs the tests, builds the site and makes `release/ultrahealth-inventory-<version>.zip`.
2. On **AWS Lightsail**: create an Ubuntu 24.04 instance (2 GB), attach a static IP, open HTTPS, point the domain at it; upload the zip and run **`sudo bash deploy/lightsail/install.sh <domain> <email>`**. It installs nginx, MySQL 8 and Python, creates the database, a random `SECRET_KEY` and `/etc/ultrahealth/inventory.env`, runs **`manage.py deploy`** (backup → migrations → triggers → levels and categories → pre-flight), starts Gunicorn and the daily backup, and gets an HTTPS certificate. Moving an existing GoDaddy database across is section B of DEPLOY.md.
3. Load real data **in the app**: the Administrator creates accounts and ticks their access, and sets the clinic GSTIN; then a Store or Checker account imports **vendors → HSN codes → products**, in that order. Templates are in `frontend/public/templates/`; files prepared from the clinic's workbooks are in `examples/prepared/` (read its README and DATA_ISSUES.md first).
4. `sudo /srv/ultrahealth/manage.sh preflight` must say **READY**. The daily backup timer is already on; switch on Lightsail's automatic snapshots too.
5. **Updates** = upload the new package + run `install.sh` again. **Rollback** = the `_previous` copies it keeps + restore the backup `deploy` made.

---

## 14. Branding

| | |
|---|---|
| Name | **Ultrahealth**: one word, lowercase "h" (all-caps ULTRAHEALTH in the logo) |
| Colours | orange `#fc7342` (actions), cream `#fff5eb` (background), teal `#31afb0` (accents), navy `#153b50` (text, sidebar) |
| Fonts | League Spartan (headings), Proxima Nova (interface; Montserrat until licensed files are in `frontend/public/fonts/`), Lora (accents) |
| Logos | `logo.png` and `butterfly.png` in `frontend/public/brand/` |

---

## 15. Change log

| Date | Version | Change |
|---|---|---|
| 2026-10-12 | 1.0.8 | **Hosting moves from GoDaddy to AWS Lightsail.** One Ubuntu 24.04 instance runs everything: nginx serves the website and passes `/api` to **Gunicorn** (systemd service `ultrahealth-api`), MySQL 8 listens on the server only, and Let's Encrypt provides HTTPS. A single script, `deploy/lightsail/install.sh`, does the first install and every update: packages, the database and its account, a generated `SECRET_KEY` in `/etc/ultrahealth/inventory.env` (root and the app only), a `_previous` copy for rollback, `manage.py deploy`, and a daily backup timer (`ultrahealth-backup`). The GoDaddy pieces are gone: `passenger_wsgi.py` (its cPanel path rewrite) is replaced by a plain `wsgi.py`, and `public_html/.htaccess` by the nginx site, which keeps the same refusals (documents, dumps, settings, dot-files, no folder listings). `gunicorn` joins `requirements.txt`. DEPLOY.md is rewritten for Lightsail, with a section on carrying an existing GoDaddy database across. |
| 2026-10-06 | 1.0.8 | **A new system installs the clinic's own lists, and nothing else.** `deploy` creates the tables, the protective triggers, the four access levels with their permissions, the clinic's row, its three categories (Clinic Consumables, Pantry, Housekeeping) and its eight units — **no vendors, no HSN codes, no products, no accounts**. (The demo vendors and products were never part of `deploy`; they belong to `seed --demo`, which only runs on a development machine.) Those lists are installed **on a first install only**: on every later upgrade the clinic's own categories and units are left exactly as they are, so one deleted on purpose does not quietly come back. Rehearsing the first day on a genuinely empty database found a dead end: the Administrator — the only account that exists on day one — could create categories but **not units or HSN codes**, which needed `product.manage`. Those two are reference lists, not stock, so they moved to a new **`list.manage`** permission held by the Administrator, Checker and Store; products and stock stay out of the Administrator's hands as before. The test fixtures now read the access matrix, the categories and the units from `manage.py` instead of keeping their own copies — which is how that drift went unnoticed. |
| 2026-10-06 | 1.0.7 | **Nothing internal is published any more.** A new `scripts/check-live-site.sh <domain>` asks the live site from outside what a stranger can reach — the guide, the database map, `.env`, backend source, folder listings, the API without signing in — and names anything that answers. (It compares what came back with the sign-in page, because a single-page site answers every unknown address with a 200.) `DEPLOY.md` now opens with what `public_html` means on cPanel: the folder is the internet, everything beside it is private, so nothing but the website ever goes in it. The package was copying `guide.html` (this document, 80 KB) and `schema.html` (the database map) into `public_html`, where anyone who guessed the address could read the schema, the access rules and the API reference; the three developer `README.md` files went the same way. The build now strips them, keeps the four import templates the app downloads, and **refuses to package** if one reappears. `.htaccess` denies them a second time — along with `.md .rtf .sql .log .db .sqlite3 .env .ini .bak .zip .gz` and folder listings — written to work on Apache 2.2 as well as 2.4. The package is 204 KB, 36 KB lighter. **The unit list cut from twenty to eight.** A fresh database now starts with **Piece, Box, Bottle, Packet, Tube, Kit, Roll, Jar** — the six the clinic's own 49 products use, plus Roll and Jar for the pantry and housekeeping. The twelve dropped were never used and most were duplicates of each other (Pack / Packet / Sachet, Ampoule / Vial, Tablet where a strip is what is handed over) or measures nothing is issued by — a litre of cleaner leaves the store as one Bottle, so Gram, Kilogram, Litre, Millilitre and Metre only invited a product whose stock could not be counted. The unit is **how stock is handed out**, never the carton it arrives in; that is the product's pack size. Units can now be **deleted** when nothing counts in them, with the same rule as categories — one in use is switched off instead, and the Lists page shows ✕ or ⦸ accordingly. |
| 2026-10-05 | 1.0.6 | **A product no longer carries a "usual vendor".** The vendor belongs to the purchase bill — one bill, one vendor, chosen when the bill is entered — so keeping a second copy on the product was a fact in two places, and the clinic does not order through the app anyway (stock is entered when it arrives). `products.vendor_id` is dropped (migration `530d13655d49`), the field is off both product forms, out of the products spreadsheet and its template, and an old file that still has a `vendor` or `vendor_gstin` column is told plainly that the vendor is recorded on the bill. Vendors themselves, their bills and their GSTINs are untouched. **Forms open as windows, not as rows that unfold.** Editing a product, correcting its stock and setting a person's access all used to push the table apart and draw the form inside a cell, squeezed into a column's width. Each is now a centred pop-up over a dimmed page — the product form 880px wide so its fields sit in proper rows, the stock form narrow — with a ✕, a Cancel, **Esc**, and the page behind held still while it is open. The product window also says *"Changing the name here changes it everywhere in the system."* Also recorded in section 7: the result of a full audit of every table's keys. |
| 2026-10-05 | 1.0.5 | **Product codes are made by the app, not typed.** The New product form no longer has a code box: saving makes the next free code for that category (`CC-0001`, `PA-0002`), so two people cannot invent the same one and nobody has to think one up. A code may still be supplied — that is how the clinic's own codes arrive through the import — and a duplicate is refused. **A product can now be made unavailable to order while it still has stock**: the old rule made a checker write the stock off first, which is why an item in use could not be taken off New request at all. Unavailable now means only "nobody may order it" — the stock, batches, reports and history are untouched, the stock list shows it greyed under a new **Not available to order** filter, and a checker can put it back with one press. It stays a checker's decision, and the Edit panel now says so to anyone else instead of simply hiding the control. **Plainer words on the product form**: "Unit of issue" → **Counted in** (how it is handed out), "Pack size"/"Pack is called" → one line reading `1 Box = 50 Piece` with a sentence showing what a bill would add, "Replacement quantity" → **Reorder level**, and "Usual vendor" now says what it is for. Also documented: `invoice_items.invoice_id` is the foreign key tying every bill line to its bill. |
| 2026-10-05 | 1.0.4 | **A way back from every page.** An order, a request, a bill, the bill form and the password screen had no back control: once opened from a link there was no way out except the menu. All of them now carry **← Back** at the top left, including their "not found" states, which previously showed a bare message with nothing to click. It steps back through history, or falls to the page's own list when there is no history. In the same sweep: the three pop-ups that ignored **Esc** (the request preview, the receipt confirmation, the category delete) now close with it through one shared `Modal`; the brand in the sidebar is a link home; a new page opens at its top instead of keeping the last page's scroll; and the browser tab carries the page name. Checked across 15 screens that every field has a label, every button has text and each page has one heading. |
| 2026-10-05 | 1.0.3 | **A category can now be deleted while nothing has used it.** Lists → Categories gains a **Delete** button, shown only when the category has no products and nobody ticked against it (`in_use` on the category), with a confirmation pop-up; `DELETE /api/categories/<id>`, Administrator only, writes `CATEGORY_DELETED` to the audit log. Anything that has been used keeps the old route — **Switch off** — because products, bills and history point at it. Categories are created and deleted, never edited. **The full numbers are off the screen for good.** The order page printed `ORD-20261005-1249F3` under the order code; that is gone, and so is every remaining full number — the request tile beside it shows the short code `3E5864`, no tooltip carries the long form, and the orders spreadsheet now has `order_code` and `request_code` columns in place of `order_no` and `request_no`. The audit log search accepts the short code people can actually see. A test now sweeps the export for the long pattern so it cannot creep back, and a screen sweep across 19 pages with all four logins confirms none is left. |
| 2026-10-05 | 1.0.2 | **Short codes on screen, full numbers in the database.** A request is stored as `REQ-20261005-7F3A2C` but shown everywhere as `7F3A2C` (its lines as `7F3A2C-2`), matching what orders already did; the short code is unique on its own, the full number is the hover text, and the API accepts either. **The order page now lists every line of the request with what happened to it** — Delivered, Rejected with the reason and who decided, or Cancelled — instead of only what was handed over, so a maker can see an item was turned down by someone else; the Orders list adds "1 not given" and the receipt text says it covers the delivered lines. **Approvals alignment fixed**: the quantity moved out of the button label (it is already in the Asked column beside it), both buttons are the same width and the Line, Asked and Decision columns are the same width in every request card, so the buttons read as one straight line down the page instead of a zigzag. Also: the Desktop guide and database map are now rebuilt by `scripts/build-docs.py`, kept with the project instead of a throw-away script. |
| 2026-10-05 | 1.0.1 | **Errors and warnings now interrupt as a pop-up** over the page (OK to dismiss, Esc or click outside), instead of a strip at the top that someone who has scrolled never sees; the form keeps what was typed. **The quantity box on New request can no longer be over-typed**: it stops at the available figure as you type, says "Only 21 Roll available" beside the box, and refuses letters — so nobody fills in 400, presses Request and only then learns there are 399. |
| 2026-10-05 | 1.0.0 | **Rebuilt on a new schema.** Categories are data, not code: any number of them, each saying whether its stock expires and whether its bills need HSN. The four fixed roles become four **access levels given per category** (`staff_access`), with each level's permissions as rows (`level_permissions`), so a person can be a maker here and a checker there. The three request-stage tables collapse into one `request_items` row per product with its own status, plus a write-once `request_item_history`; orders collapse to one table with a short code (`AF470D`, unique on its own) and a separate write-once acknowledgement. Purchases gain **packs** (packs × units per pack, so stock is always the issue unit), **payments** as their own table (part payments), bills for **every category**, and vendor **contacts**. Every table that can be updated carries created_by/updated_by with timestamps; write-once tables carry created_by only. Screens follow: New request has no Add button (type quantities, press Request, confirm in a pop-up), quantities are capped at what is available, out-of-stock rows are shown greyed, categories have their own tabs, the Orders list is stripped to the order code, and the Users screen ticks a level per category. 25 tables → 23 (22 plus the migration bookkeeping table), 54 tests on both databases, walked through on screen with every login. |
| 2026-09-23 | 0.10.1 | One word everywhere: the Orders column "Accepted by" is now **Approved by**, matching the Approvals screen, the status badges, the audit log and the spreadsheet export. Also corrected the request-flow diagram in section 3, which still said a manager may change the quantity. |
| 2026-09-23 | 0.10.0 | **Acknowledgement of receipt.** On Orders, the person who raised the request presses **Acknowledge receipt**; a pop-up lists what was handed over, takes an optional note (e.g. "one box damaged") and records the confirmation with their name and the time. Only they can confirm it, only once; everyone else sees *Received ‹date›* or *waiting for ‹name›*, and the spreadsheet export gains two columns. Kept in its own write-once table `order_acknowledgements` (composite foreign key so only the order's requester can be the confirmer), leaving orders themselves unedited, and written to the audit log as `ORDER_ACKNOWLEDGED`. |
| 2026-09-23 | 0.9.3 | Orders screen: added an **Accepted by** column (the manager who approved the request), and the **For** and **Requested by** columns now appear only for people who see everyone's orders — on your own history they were always your own name. |
| 2026-09-23 | 0.9.2 | **History merged into Orders**, as they are the same thing: the Orders screen now has **Delivered orders** and **Rejected & cancelled** tabs, the History menu entry is gone, and `/history` opens Orders. **My requests now holds only what is still waiting** (pending approval, or approved and waiting to be handed over); once handed over a line moves to Orders under its order number, and a rejected or cancelled one to the Rejected & cancelled tab. `GET /requests/mine` returns only those two stages. |
| 2026-09-23 | 0.9.1 | **Staff can now see what they may order.** New request became a table: 10 products a page (Previous/Next), search and segment tabs, each row showing the available quantity and a box for how many are needed, with **Add**; the chosen products are listed below, **Confirm** shows a pop-up listing them, and OK sends it for approval. Staff still see no stock levels, costs or prices, and only in their ticked segments. **Approval is now approve-or-reject only**: the manager can no longer change the quantity (the box is gone); a rejection requires a reason, with the box prompting "Please mention the reason and the available quantity", and the person raises the request again with that quantity. `GET /items/search` gained paging and works with no search text. |
| 2026-09-23 | 0.9.0 | **Orders.** A finished request now gets its own order number (`ORD-20260923-9514D8`), created by the app the moment nothing in that request is left waiting — delivered in two goes still means one order. New `orders` / `order_lines` tables, both write-once (triggers), holding no duplicate figures: each line points at its delivered request line. Every order is written to the audit log (`ORDER_CREATED`), shown on the request page, and listed on a new **Orders** screen (search, segment and month filters, products shown per order, spreadsheet export; Staff see only their own and can't export). Requests already delivered were given order numbers by the migration (9 on the local database). Fixed along the way: typing in a filter box sent one call per letter and an older answer could overwrite a newer one, so the list stayed unfiltered (Orders and Purchase bills). |
| 2026-09-22 | 0.8.4 | Phones: the menu is now a **left-side drawer** (slides in over the page with a dimmed background; closes on tapping outside, choosing a page, ☰ or Esc; the page behind doesn't scroll) instead of a block stacked at the top of the screen. Computers unchanged. |
| 2026-09-22 | 0.8.4 | Fixed (local dev): `./start.sh` stopped itself a second after starting when run without `LAN=1` (a shell check on an unset variable), which also made the ngrok link go dead. Vite now listens on 127.0.0.1 as well (it was IPv6 `::1` only, which ngrok couldn't reach). |
| 2026-09-22 | 0.8.4 | Local dev: `LAN=1 ./start.sh` serves the site to other devices on the same Wi-Fi; the Vite dev server accepts ngrok / Cloudflare tunnel addresses, so `ngrok http 5173` works without VS Code's GitHub sign-in. Dev-only; the live site is unchanged. |
| 2026-09-22 | 0.8.4 | **Unknown details show as "—", never invented.** A missing batch number is stored empty (NULL) instead of OPENING / OPENING-SEP26 / AUTO-… / DEMO-…; the migration turns those placeholders into empty ones (58 on the local database; real bill batch numbers kept). Bills may leave a line's batch number blank. Missing expiry, cost, GSTIN, free qty, discount and decider now all show "—". Prepared `clinic_products.csv` has blank batch numbers. **Sign-in ends after 24 hours** (was 12; capped at 24) and the open screen signs out by itself at that moment with a message; closing a tab signs out. Tests for both on SQLite and MySQL. |
| 2026-09-22 | 0.8.3 | **Searchable drop-downs** (`SearchSelect` in `components.jsx`) for products, vendors, HSN codes, units and audit actions: on bills, product forms, the bills filter and the audit log. **Alignment pass over every screen**: form boxes line up at their labels (help text under a box no longer pushes it up, e.g. New request), buttons sit level with the boxes, all boxes are the same height (date boxes were taller), labels stay on one line (long ones shortened, explanation moved under the box), page-header buttons centred, table controls compact and vertically centred, Users table buttons stacked so nothing runs off the card. Checked by measuring every form row on every screen at 1280, 1024 and 420 px wide. |
| 2026-09-22 | 0.8.2 | Fixed: signing in as someone else in another tab replaced the sign-in of every open tab, so a tab still showing "Clinic Admin" was really acting as the other person (Users page said "You do not have permission"). The sign-in is now kept per tab; a new tab asks to sign in, and closing the browser signs out. |
| 2026-09-22 | 0.8.1 | Owner feedback on Inventory: **☰ hides the menu** to use the full screen (remembered per browser); the **menu scrolls separately** from the page; **filters** show which products are Low on stock / Expiring within 30 days / have Expired stock (expiry filters open the batch tables automatically); **batches shown as a full-width table** ("not recorded" instead of a bare dash for missing expiry; "used next" marks the batch that goes out first); Stock and Edit forms open full width under the product. |
| 2026-09-22 | 0.8.0 | Added **DATA_ISSUES.md**: a checklist of the clinic data problems for the owner to fix later (Kiya GSTIN, Parekh Telangana GSTIN, Care Pharmacy/Spectra double registrations, HSN codes and rates, guessed units). No code change. |
| 2026-09-22 | 0.8.0 | **Clinic consumables schema from the clinic's own workbooks** (`examples/`, every sheet analysed). **Fixed lists**: vendors (GSTIN with check digit; state and PAN derived; composition-scheme flag), units, HSN codes with GST rates, the clinic's own GSTIN. **Stock per batch** (batch no + expiry; quantity = its ledger rows) with **earliest-expiry-first** delivery; expired stock isn't available. **Purchase bills** (Clinic Consumables; Store Keeper + Manager): drafts, calculated CGST/SGST/IGST, free qty, discount, round-off, printed-total check, post = stock in per batch, cancel, paid/unpaid; machines as non-stock lines. **Prices** come from bills: batch cost → **stock value** report; **monthly usage report** (like the workbook's day-by-day sheet) with CSV. Imports refuse unknown or old columns. Prepared import files (23 vendors, 24 HSN codes, 49 products) in `examples/prepared/`. Migration carries existing products into an opening batch and turns typed units/suppliers into list entries. Two real bills re-entered to the paisa as tests. Walkthrough with every login. |
| 2026-09-21 | 0.7.1 | **Staff never receive stock figures, prices or inventory value.** Their request lists and details no longer include the product's stock count, stock snapshots or IP addresses (a test scans every Staff response). Fixed: the request details page showed the approval note on the delivery step too (all roles); each step now shows its own note. Fixed: two backups made in the same second overwrote each other (file names now include microseconds). TODO: prices must stay hidden from Staff. |
| 2026-09-21 | 0.7.0 | **Four fixed roles**; the Roles screen is gone. **Segment access per Staff account** (checkboxes at account creation, start empty, at least one; editable). **Staff have no Inventory page**: they search products when requesting and see only the available quantity; History shows only their own requests. **Several products per request** (search → Add → Send; `request_batches` + numbered lines; Approve all / Deliver all, all-or-nothing). **Only Managers change quantities**, up or down; a changed quantity or a rejection needs a note (DB-enforced); Store Keepers deliver exactly the approved quantity. **Can't approve more than available** (stock minus already-approved). **Stock single source of truth enforced by the database** (ledger triggers; new products start at 0). Product **Edit** / **Remove from list**; unit box no longer pre-filled; role change asks for confirmation; stock warnings on Approvals/Deliveries. Found through a full walkthrough with every login. Migrations carry old data forward; the MySQL run caught and fixed two migration bugs that SQLite hid. TODO.md added. |
| 2026-09-21 | 0.6.0 | **Deployment readiness**: release package (`build-release.sh`, checksums, no secrets), `DEPLOY.md` runbook, `manage.py deploy` (backup → migrate → pre-flight), `preflight`, `backup` (fixed so backups restore on shared hosting), CSV import/export for products and staff (all-or-nothing, row-numbered errors, Excel/Indian date formats), forced password change for temporary passwords, version endpoint, pinned requirements, Python 3.10+ verified. Full GoDaddy rehearsal against MySQL. Brand spelled **Ultrahealth**. |
| 2026-09-21 | 0.5 | Replacement quantity = low-stock level (one field). Store Keeper can add stock and deliver own approved requests. MySQL 8.0.31 installed locally; the whole test suite runs on MySQL too. `APP_ENV`. |
| 2026-09-21 | 0.4 | Sign in with a username (or email). Local test logins with password `test`. |
| 2026-09-21 | 0.3 | Roles redefined; separate tables per segment with ordered on, expiry, brand, supplier; expiry flags; this guide created. |
| 2026-09-21 | 0.2 | Database hardening: composite FKs, CHECK rules, `atomic()` transactions, row locks, strict mode, triggers, migrations, consistency check. |
| 2026-09-21 | 0.1.1 | `start.sh`. |
| 2026-09-21 | 0.1 | First version: 3 segments, requested → approved → delivered, approval rule, audit log, React + Flask, GoDaddy plan, branding. |
