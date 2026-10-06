# VK Platform — overview for cable & ISP operators

A web-based billing, CRM, and provider automation system built for Indian LCOs who run Hathway (cable), Railtel (broadband), IPTV (ANT), and OTT (SmartPlay) from one office. One customer household can hold multiple connections; one ledger shows what they owe and what was collected.

---

## Who it's for

- Multi-provider shops (cable + broadband + IPTV + OTT on the same customer)
- Teams with office staff and field collectors
- Operators who want renewals and status checks on dealer portals without logging in manually for every box

---

## Customer & connection management

- Single customer record — name, phones, area/locality, address, GPS pin, notes, customer code
- Multiple connections per household — Railtel login, Hathway STB/VC, IPTV, OTT, each with plan, expiry, status
- Search & filters — by name, phone, STB/Railtel ID, area, provider, due/expiring/expired, follow-up, terminated STB
- Hybrid households — Hathway + Railtel on one account; renew and collect at customer level
- Custom / bundled pricing — household custom plan or per-connection overrides (incl. GST on plans)
- Account statement — running balance view (bills up, payments down) plus precise bill–payment allocation for renewals

---

## Billing & collections

- Bills — period charges, opening balance, adjustments; link to connections where needed
- Collect payment — cash, UPI, cheque, etc.; default amount = full outstanding (partial pay supported; remainder stays on due)
- Optional "renew after collect" — queue provider renewal when payment is recorded (with staff approval before portal runs)
- Collect later / follow-up — renew on portal now, chase payment separately
- Bill checker — generate bills when paid periods end, with clear skip reasons (no silent failures)
- Receipts & bill print — payment receipts and bill documents from the UI
- Payment follow-up lists — who renewed but hasn't paid; quick collect from mobile lists

---

## Provider portal automation (Railtel & Hathway)

- Job queue + single worker — one portal login at a time; no double-spend on dealer wallet
- Actions — renew, check status, batch status for expired follow-up lists, and related portal tasks
- Confirm before run — sensitive jobs wait for Confirm & run (collectors can be blocked from portal access)
- OTP step — when Railtel/Hathway asks for OTP, staff enter it on the job page; UI can auto-refresh while waiting
- Simulate vs live — rehearse full flow without touching portals; switch to live for production
- Scheduled sync — optional nightly/scheduled bulk status refresh from provider lists
- Provider dashboard — online/expired views, wallet-oriented workflows, sweep progress
- Railtel specifics — term plans (e.g. x3 / x6 / x10 months with correct validity), My Subscribers import, invoice PDF storage and optional WhatsApp send
- Hathway specifics — STB list, pack management path automation, VC/STB-centric customer views

---

## IPTV (ANT) & OTT (SmartPlay)

- Dedicated IPTV / OTT sections — manage connections, plans, subscribe/renew flows tied to your partner portals
- Sync jobs — pull partner data where integrated (e.g. OTT sync)
- Same customer ledger model as cable/broadband where applicable

---

## Mobile experience (collectors)

- Mobile v2 UI — card-based home, customer search, expiring/expired, unpaid renewals, pay QR
- Field agents — GPS ping on collect/visit, day sheet, collections tied to agent
- WhatsApp shortcuts — payment received, expiry reminders, complaint alerts (where enabled)
- Classic desktop UI — full tables for office; switch between layouts

---

## Customer self-pay (shop QR)

- Public pay page — customer enters mobile or STB/Railtel ID, sees due, pays via UPI app or on-screen QR
- Staff confirmation — payment marked by customer → office confirms UPI → receipt + optional renewal job
- Settings — UPI VPA, payee name, enable/disable public portal
- Works with your own domain (e.g. HTTPS via Cloudflare Tunnel)

---

## Complaints & operations

- Complaint log — create, assign agent, notes, mark fixed
- WhatsApp alerts — optional notify technicians group / agent / customer
- Activity log — audit trail of important actions

---

## Plans, access & security

- Plans / packages — per provider, prices, GST; used for billing and renewals
- Multi-agent login — admin + collectors with granular permissions (view, collect, portal, packages, complaints, WhatsApp, settings)
- Provider scope — Railtel-only or Hathway-only agents if needed
- Area / locality — limit which customers a collector sees

---

## Technical highlights (for IT-minded partners)

- SQLite database — single file backup
- Integer paise — no floating-point money bugs
- Background worker — renewals/status never block the web UI
- REST API — same operations as UI (for bots/scripts later)
- Optional demo instance — separate DB, simulate mode, training login (demo / demo123) on another port

---

## Typical daily flow

1. Customer pays (field or office) → Collect payment (full or partial due).
2. Tick queue renewal → job waits for Confirm.
3. Worker renews on Railtel/Hathway → new bill period → payment applied → due updates.
4. Expiring/expired lists and optional scheduled status refresh keep the office list accurate.

---

## What this is not (for this summary)

- Not a replacement for Hathway/Railtel dealer portals — it drives them safely from your data.
- Not tied to one billing back-office product; it's built for operators who run multiple upstream brands from one shop.

---

VK Digital Hub · September 2026
