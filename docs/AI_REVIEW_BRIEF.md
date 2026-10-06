# AI Review Brief: VK Digital Operations Platform

> **For the reviewing AI:** this document describes a real, working software product so you can give an independent, critical review. You do not have the source code. Everything you need is below. Read the whole brief, then follow **Section 9 (Review instructions)** and answer in the **Section 10 output format**.
> Be candid. The owner wants to know whether this can become a product sold to other companies, and what would stop it. Do not just agree with the plan in Section 7 — challenge it.

---

## 0. Document metadata

| Field | Value |
|---|---|
| Product | VK Digital Operations Platform ("VK Platform") |
| Owner | VK Digital, Tiptur, Karnataka, India (a local cable TV + broadband operator) |
| Document date | 2026-10-04 |
| Status | In daily production use by one company (the owner) |
| Purpose of review | Assess product quality, fitness for productization (multi-company SaaS), modularization plan, risks, and market viability |
| Built by | The owner with heavy use of AI coding assistants |

---

## 1. Business context

### 1.1 The industry
- In India, local operators ("LCOs" / franchise partners) resell services from large providers:
  - **Railtel (RailWire)** — government-backed broadband. Operators manage subscribers through a dealer web portal (`ka.railwire.co.in`), top up from a prepaid dealer wallet.
  - **Hathway** — cable TV (set-top boxes / STBs). Operator manages boxes, packs and renewals through a dealer portal.
  - **ANT IPTV** and **SmartPlay OTT** — smaller add-on TV/streaming services, also via dealer portals.
- The providers' portals are built for manual clicking. They have **no public APIs**. They use captchas, session timeouts, popups and layouts that change without notice.
- Operators collect money from end customers mostly **in cash or UPI, door to door**, through field agents (collectors). Customers often pay late or partially; operators carry running balances ("dues").
- Many operators previously used a desktop billing tool called **Bix (Bix42)**; this platform imports and reconciles that legacy data.

### 1.2 The owner's operation (current scale)
| Metric | Value |
|---|---|
| Customers | 1,343 |
| Railtel broadband connections | 454 |
| Hathway cable boxes | 797 |
| ANT IPTV connections | 21 |
| SmartPlay OTT connections | 13 |
| Users | 3 (1 admin/owner, 2 field collectors) |
| Bills in system | ~2,100 |
| Payments recorded | ~760 |
| Portal automation jobs run | ~2,500 |

### 1.3 Problems the platform solves
1. One place for every customer and all their services (one customer can have broadband + cable + IPTV).
2. Accurate dues/ledger instead of paper or Bix.
3. Field collection on mobile, restricted to each agent's areas.
4. Automating repetitive portal work (renewals, status checks, subscriber sync) that previously took hours of clicking.
5. Customer communication on WhatsApp (payment confirmations, reminders, expiry alerts).
6. Visibility on agents: cash collected vs. handed over, location during duty, activity.

---

## 2. Functional inventory (what exists today)

| Area | Features |
|---|---|
| Customers & connections | Multiple connections per customer across providers; areas/sub-areas; owner/free tags; custom plans; discounts; statements; search by name/phone/STB/Railtel id |
| Billing & ledger | Bills per cycle/renewal/custom plan; GST (inclusive/exclusive); amounts stored as integer paise; totals rounded **up** to whole rupees; opening balances; balance adjustments (permission-gated); bill checker; ledger reconciliation |
| Payments & receipts | Cash/UPI/scanner/bank/cheque/adjustment; backdating (permission-gated); printable A4 receipt with logo, amount in words, balance due; payment deletion with ledger rebuild |
| Collections workflow | Mobile "v2" UI for agents; follow-ups; unpaid renewals; "expiring tonight"; "expired yesterday / 2 days / 7+ days"; Railtel term-plan month view; each list restricted to the agent's territory |
| Provider automation | Background job queue with retries/priorities. Actions per provider: **Railtel** renew, status, clear session, download bill, wallet, online list, full subscriber sync. **Hathway** renew, status, retrack, activate, deactivate, remove/terminate, wallet. **ANT IPTV** renew, status, subscribe, wallet. **SmartPlay OTT** renew, status, wallet, sync. Batch status checks (one login, many accounts). Scheduled sweeps. |
| WhatsApp | (a) "Edit first" links (wa.me) that open WhatsApp with a pre-filled message. (b) Office auto-send: browser automation of WhatsApp Web on the office PC sends messages without manual editing. Auto messages: payment confirmation, renewed, reminders, expiring tonight, complaint alerts to technician group/agent/customer. **All message wording is editable in Settings → WhatsApp templates** (14 templates, placeholders like `{name}`, `{amount}`, live preview). |
| Complaints | Log, assign technician, follow-up notes, mark fixed; WhatsApp alerts |
| Field agents | On-duty toggle, GPS location trail (visible to admin only), per-agent activity log, admin map |
| Settlements | Agent cash handover to office with proof photos and line items |
| Inventory | Stock items, receipts into stock, usage in the field |
| Customer self-pay | Public `/pay` page via shop QR sticker: customer finds account by mobile/STB, pays by UPI, staff confirm in a queue |
| Legacy (Bix) | Import Bix files, sync dues from Bix, history browser |
| Reports/exports | CSV/Excel exports of lists |
| Access control | Per-user permissions (16 flags, below); provider scope per agent (Railtel / Hathway / both); area assignment (territory) |

### 2.1 Permission flags (current)
`customers_view`, `customers_edit`, `payments`, `payment_date`, `change_due`, `bills`, `portal_actions`, `providers`, `packages`, `activity`, `complaints`, `customer_whatsapp`, `bix_sync`, `inventory`, `agents`. Admin role has all. Collector default: `customers_view`, `payments`, `complaints`, `inventory`.

---

## 3. Technical architecture (as-is)

| Aspect | Current state |
|---|---|
| Language / framework | Python 3.12, FastAPI, Jinja2 server-rendered HTML, small vanilla JS |
| Size | ~26,000 lines Python, 151 HTTP routes, 85 HTML templates |
| Largest files | `routes/pages.py` 4,300 lines; `repo.py` 2,500; `upstream/jobs.py` 1,600; `billing.py` 1,300; `settlements.py` 1,200; `whatsapp_send.py` 1,200 |
| Database | **One SQLite file**, 33 tables, hand-rolled migrations (schema version 40) |
| Background work | Threads inside the web server process: job worker, schedulers (expired-status checks, Bix schedule), WhatsApp send worker |
| Portal robots | Playwright (headless Chromium) scripts in a sibling folder (`railtel_debugger/vk_agent`), imported dynamically. Captcha solved by OCR. One dealer login per provider (multi-account support partly exists for Railtel). |
| WhatsApp auto-send | Playwright controlling a persistent WhatsApp Web browser profile on the office PC; single worker thread |
| Configuration | Two `.env` files (workspace + project); provider credentials in `.env`; some settings in a DB `settings` table (UPI, templates, group name) |
| Auth | Cookie session (HMAC-signed), PBKDF2 password hashes, per-user permissions. Fallback default password/secret exist in code if env is missing. |
| Hosting | One Windows PC in the office, exposed on a public domain over HTTPS; restarted manually; dev server with auto-reload |
| Files | Local disk: invoice PDFs, settlement proof photos, screenshots, WhatsApp browser profile |
| Tests | **No automated test suite.** ~50 one-off scripts for imports, fixes, checks |
| Multi-company | **None.** No company/tenant column anywhere; settings and branding global; some VK-specific text hard-coded (company name in messages, town name, Bix paths) |

### 3.1 Database tables
`activity_log, agent_areas, agent_locations, agent_settlement_lines, agent_settlement_proofs, agent_settlements, agents, bill_payments, bills, bix_history_customers, bix_history_imports, bix_history_txns, bix_sync_batches, complaints, connections, customers, hathway_expiry_batches, inventory_items, inventory_receipts, inventory_usage, packages, pay_intents, payments, provider_status, railtel_invoices, railtel_online_rows, railtel_online_snapshots, railtel_subscriber_rows, railtel_subscriber_snapshots, settings, sync_sweeps, upstream_jobs`

Notes:
- `connections` has a generic `provider` column (good base for modularity) but also provider-specific columns (`subscription_expiry`, `portal_account_id`, `link_state` for Railtel; `hathway_pack_name` for Hathway).
- Provider-specific logic is spread through code as `if provider == "railtel"` branches and fixed provider lists.

---

## 4. Recent real incidents (evidence of fragility / quality)

These happened in the last few days and were fixed; they show the kind of problems the product faces.

1. **Agent territory leak** — collector A saw collector B's customers on two list pages because the territory filter was not applied on those queries.
2. **Missing permission** — "change due" (balance correction) was available to collectors; there was no permission flag for it.
3. **WhatsApp auto-send silently failing for days** — WhatsApp Web UI change left the automation on the wrong panel; all office sends failed until noticed.
4. **Railtel term-plan expiry missing** — for 6/10/12-month plans the true end date is on a profile page. (a) A full subscriber sync scraped it but then crashed while saving (bad import) and rolled back; (b) the single-account status check scraped it but dropped the field before returning; (c) the portal lists many term plans under a generic package name so the robot didn't know to read it. Result: only 4 of 191 term customers had a correct expiry, so many were missing from "expiring tonight".
5. **Customer-facing receipt** showed the owner's personal name instead of the company brand and leaked an internal reconciliation note.

---

## 5. Strengths (owner's view)
- Deep fit to a real niche workflow; used daily; replaces hours of manual portal work.
- Handles messy realities: partial payments, legacy dues, term plans, owner/free accounts, territory rules.
- Generic connection model; permission system; editable message templates; audit/activity log.
- Very fast iteration with AI assistance.

## 6. Known weaknesses (owner's view)
- Single company, single machine, single SQLite file.
- No tests; very large files; hard-coded company details.
- Portal robots and WhatsApp Web automation are brittle and depend on one PC.
- WhatsApp Web automation is against WhatsApp terms of service.
- Secrets in `.env`; default fallbacks in code.
- Location tracking of agents done without informing them (owner's choice) — likely unacceptable for a product.
- Relies on provider portals that have no APIs and may prohibit automation.

---

## 7. Proposed plan (to be reviewed — challenge it)

### 7.1 Productization requirements identified
1. **Multi-company data isolation** — company id on all data; Postgres with row-level security long term (or database per company); automated isolation tests.
2. **Users & login** — users belong to a company; roles: platform owner / company admin / agent; OTP or reset; 2FA for admins; login rate limits; remote logout; remove default secrets.
3. **Encrypted per-company provider credentials** in DB instead of `.env`.
4. **Automation as a separate worker service** — real queue; per-company concurrency and portal sessions; server-side captcha; per-company screenshots; rate limits; **portal health monitoring** with alerts and versioned robots.
5. **WhatsApp** — move to official WhatsApp Business Cloud API (direct or via a BSP such as Gupshup/Interakt/WATI); per-company numbers and approved templates; keep wa.me links as fallback.
6. **Hosting/ops** — cloud, Docker, separate web/worker, staging, backups with point-in-time restore, error tracking, uptime alerts, Alembic migrations, S3-style per-company file storage.
7. **Remove hard-coding** — per-company branding, receipt prefix, GST %, due days, UPI, timezone, language; Bix as one optional importer.
8. **Tests and structure** — tests for ledger math, rounding, territory scoping, permissions, tenant isolation; split large files; CI.
9. **Business/legal** — generic CSV/Excel onboarding importer and setup wizard; SaaS billing with per-module pricing and usage limits; platform owner console; DPDP Act 2023 compliance (consent for location tracking, data export/delete); GST-compliant receipts; terms covering portal automation with the customer's own dealer credentials.

### 7.2 Modularization design
- **Core (always on):** companies, users, permissions, customers, areas, billing/ledger, payments/receipts, activity log, settings, templates, reports.
- **Provider modules (pluggable):** Railtel, Hathway, ANT IPTV, SmartPlay OTT; future others (GTPL, Den, ACT, Mobicable, etc.). Each declares: label, connection-id format, plan catalog/import, supported actions, portal robot, expiry rules, credential settings, scheduled jobs, UI pieces, permissions, default WhatsApp templates.
- **Feature add-ons:** WhatsApp auto-send, self-pay UPI, field agents & location, settlements, inventory, complaints, importers (Bix), GST invoicing.
- **Mechanism:** per-company `enabled modules` table; one check gates menus, routes, workers/schedulers, permissions, templates, importers. Agent provider scope generalised to any subset of enabled providers. Provider-specific columns/tables move into module-owned storage with company id.
- Example bundles: Hathway-only cable operator; Railtel-only ISP; full operator.

### 7.3 Roadmap (rough, one developer + AI assistants)
| Phase | Goal | Work | Time |
|---|---|---|---|
| 0 Harden | Safe to copy | Tests (money, billing, permissions), branding/settings UI, remove default secrets, backups, split files | 2–3 weeks |
| 1 One copy per company | 2–5 pilot operators | Docker on cloud (web + worker + browser), module flags, encrypted credentials, generic importer, setup wizard, monitoring | 4–6 weeks |
| 2 True multi-company | One shared platform | Postgres + isolation + migrations, object storage, queue workers with per-company sessions, owner console, onboarding, SaaS billing | 8–12 weeks |
| 3 Scale | Grow | WhatsApp Cloud API, more providers, offline mobile app, analytics, compliance docs | Ongoing |

### 7.4 Open decisions
1. Separate copies for pilots first, or straight to multi-company?
2. Keep WhatsApp Web robot for pilots, or adopt the official API now?
3. Which providers do first outside customers need?
4. Is Bix used by other operators?

---

## 8. Constraints and assumptions
- Target customers: small/medium local operators in India (likely 200–5,000 subscribers each, 1–15 staff), price-sensitive, mostly mobile-first staff, mixed digital literacy, regional languages (Kannada, etc.).
- Team: essentially one owner-developer using AI coding tools; limited budget.
- Providers may change portals at any time; no official partnership/API access today.
- Must keep working for VK Digital's own daily operations throughout.

---

## 9. Review instructions (for the reviewing AI)

Review as a **senior SaaS product architect + Indian telecom/ISP-operator business analyst + security reviewer**. Specifically:

1. **Product assessment** — Is this a genuinely valuable product for Indian local cable/broadband operators? What is the strongest value proposition? What is missing that competitors or operators would expect (e.g. customer app, online payment gateway, GST invoices, SMS, accounting export, analytics)?
2. **Market & competition** — What kinds of existing tools serve this market (LCO billing software, ISP billing/RADIUS systems, MSO-provided apps)? Where does this product fit or differ? Is portal automation a moat or a liability?
3. **Architecture review** — Evaluate the current architecture for a multi-company SaaS. Is the proposed path (Phase 0→1→2) right? Would you choose shared DB with tenant id + RLS, DB-per-tenant, or instance-per-tenant for this scale and team? Is the in-process thread model acceptable short term?
4. **Portal automation risk** — Assess technical fragility, legal/ToS risk, account-blocking risk, captcha issues, and how to design robust, monitorable, versioned adapters. Suggest alternatives (official partner APIs, RPA vendors, human-in-the-loop fallbacks).
5. **WhatsApp strategy** — Evaluate WhatsApp Web automation vs. Cloud API/BSP for cost, compliance and reliability at multi-tenant scale.
6. **Security & compliance** — Top risks for a product holding customer PII, dealer credentials, payments, and agent GPS data in India (DPDP Act 2023, consent, retention, breach handling). Rank them.
7. **Modularization design** — Critique the core / provider module / add-on split and the enable-per-company mechanism. Suggest the interface a provider module should implement, and how to handle provider-specific data.
8. **Plan realism** — Are the timelines realistic for one developer with AI tools? What would you cut, reorder or add? What is the minimum viable product for the first paying external operator?
9. **Business model** — Suggest pricing structure (per subscriber, per agent, per module, setup fee), go-to-market for small operators, and onboarding/support needs.
10. **Top risks & kill criteria** — What could make this fail? What should be validated first, cheaply, before investing in Phase 2?

Ground your answers in the facts above; state clearly when you are assuming something. Prefer specific, actionable recommendations over generic advice.

---

## 10. Required output format

Respond with these sections, in order:

1. **Verdict (≤5 sentences)** — Is this a viable product? Overall readiness score 1–10 for (a) current single-company use, (b) selling to other operators today, (c) after the proposed plan.
2. **Scorecard table** — rate 1–10 with one-line justification each: Product value, Feature completeness, Code/architecture quality, Scalability, Reliability, Security, Compliance, Modularity, Plan realism, Market potential.
3. **Top 10 risks** — ranked, each with likelihood (H/M/L), impact (H/M/L), mitigation.
4. **What to change in the plan** — keep / change / add / remove, with reasons.
5. **Recommended architecture** — tenancy model, worker/queue design, provider module interface (pseudo-code welcome), data model changes.
6. **MVP for first external customer** — exact scope and a week-by-week sequence.
7. **Business model & go-to-market** — pricing proposal and first 90 days.
8. **Questions you would ask the owner** — anything that would change your recommendations.
