# VK Platform

A small billing and CRM system for VK Digital Hub — the parts of CableWay that
actually get used, with the provider automation you already own doing the work.

Two providers: **Railtel / Railwire** (broadband) and **Hathway** (cable TV).
Renewals run through `railtel_debugger/vk_agent`, the same Playwright code the
Telegram bot uses.

## The idea

CableWay keeps one record per provider account. This keeps one record per
**customer**, and hangs their connections off it — so a household with a Hathway
STB and a Railtel broadband line is one person with two connections, one ledger
and one outstanding balance.

Everything that touches a provider portal goes through a **job queue** rather than
running inside a web request. A renew takes minutes (login, captcha, page waits)
and must never run twice at once on the same dealer login, so a single background
worker drains the queue one job at a time.

## The daily loop

```
Customer pays
   -> record the payment on their page, tick "queue a renewal"
   -> the job waits in "needs approval" (nothing has touched the portal yet)
   -> you press Confirm & run
   -> the worker logs in to Railtel/Hathway and renews
   -> a bill is created for the new period at the plan price
   -> the payment is applied to the bill, dues update
```

Renewing early never burns paid days: if the connection still has time left, the
new period starts the day after the old expiry. An expired connection starts today.

## Setup

```powershell
cd vk_platform
pip install -r requirements.txt
copy .env.example .env        # then edit the password and secret
python scripts/import_seed_data.py --reset
python run.py
```

Open http://127.0.0.1:8800 and sign in as `admin` with `VK_PLATFORM_PASSWORD`.
Create field collectors on **Settings → Agents**. A collector can view customers
and take payments; portal actions and Bix updates stay off unless you tick them.

To use it from your phone on the same wifi: `python run.py --host 0.0.0.0`.

### Simulate vs live

`VK_PLATFORM_UPSTREAM_MODE` starts at `simulate`, which means **no provider portal
is ever contacted**. Jobs still run, bills are still created from plan validity, so
you can rehearse the whole flow without spending money on provider wallets. The
header shows a SIMULATE / LIVE badge, the jobs page explains the mode, and every job
that ran this way is tagged **simulated** — a `done` job with that tag reported
success without ever opening a portal, and its expiry came from the plan's validity
rather than from the provider.

Switch to `live` only when you are ready. Live mode needs the `vk_agent`
dependencies (`playwright`, `pytesseract`, `Pillow`, plus Tesseract OCR installed)
and the provider credentials in `vk_digital_hub/.env` — `RAILWIRE_USER`,
`RAILWIRE_PASS`, `HATHWAY_USER`, `HATHWAY_PASS`. Expect roughly 20–30 seconds per
job: each one is a fresh browser, login and CAPTCHA solve.

**A variable already in the environment beats both `.env` files.** That is what keeps
`scripts/smoke_test.py` offline — it exports `VK_PLATFORM_UPSTREAM_MODE=simulate`
before the config loads, and refuses to start if it ever finds itself in live mode,
because its fixtures use invented ids that would send real portal logins looking for
boxes that do not exist.

## What the seed importer loads

From `cableway_automation/data/`:

| File | Becomes |
| --- | --- |
| `railtel_plans_catalog.csv` | Railtel plans, with term pricing computed from the ` xN` suffix |
| `cableway_packages_hathway_only.csv` | Hathway bouquets |
| `cableway_plans_export.csv` | fills any plan the two files above miss |
| `cableway_hathway_generated_v2.csv` | Hathway customers and their STBs, grouped by customer code |
| `railtel_customers.csv` | Railtel accounts, attached to a Hathway customer when the phone matches |

Bix outstanding balances become one "Opening balance" bill per customer, so dues
carry over instead of starting from zero.

Current load: 924 customers, 996 connections, 798 plans, ₹8.9L opening dues.
25 households were merged across both providers.

## Bix update while agents still collect there

Until collection moves fully onto this app, upload today’s Bix **Customer details**
export on **Settings → Bix update**. The file is the source of truth for what each
household owes — nothing is fetched from the Bix website.

Preview matches each Bix customer (code, then STB, then unique phone) to a row
here, then Apply posts a bill or a credit so net due equals Bix Due Amount. Tick
“create missing” only if you want households that are in Bix but not here to be
added. Past uploads stay listed so you can see what was applied.

## Railtel term plans

Railtel's catalog lists a monthly amount and 30-day validity even for term plans.
A trailing ` xN` means pay N months up front, and the term carries free days:

| Suffix | Price | Validity |
| --- | --- | --- |
| none | monthly amount | 30 days |
| ` x3` | monthly × 3 | 100 days (90 + 10 free) |
| ` x6` | monthly × 6 | 210 days (180 + 30 free) |
| ` x10` | monthly × 10 | 360 days (300 + 60 free) |

## Layout

```
app/
  config.py            settings from .env
  db.py                SQLite schema (plain sqlite3, one file you can copy to back up)
  money.py             integer-paise amounts, tolerant date parsing
  plans.py             Railtel term pricing rules
  billing.py           bills, payments, FIFO ledger reconciliation, bill checker
  repo.py              read queries for the UI
  auth.py              multi-agent login, hashed passwords, access control
  bix_sync.py          parse a Bix Customer details file and align dues
  upstream/
    providers.py       adapters over vk_agent's check_* functions
    jobs.py            job queue and the background worker
  routes/
    pages.py           the web UI
    api.py             JSON API, for wiring the Telegram bot in later
scripts/
  import_seed_data.py  load the CSV exports
  smoke_test.py        end-to-end check of the money flow on a scratch database
  check_pages.py       hit every page of a running server
```

## Money is stored in paise

Every amount in the database is an integer number of paise. Repeated billing
arithmetic on float rupees drifts; this cannot. Use `money.to_paise` /
`money.fmt_rupees` at the edges.

## The ledger

The customer page leads with an **account statement** in the style Bix uses: one
running list where a bill pushes the balance up, a payment pulls it down, and the
balance column is what the customer owed after that row. That is the view you want
when a customer asks "how much do I owe" — no allocation to reason about.

Underneath it is more precise than Bix, because renewals need to know *which* period
was paid for. Three tables, and allocation is always recomputed rather than
incremented:

* `bills` — what we charged, for one service period
* `payments` — what we received, independent of any bill
* `bill_payments` — allocation, oldest bill first

Recording a payment before its renewal bill exists is fine: the money sits as
advance and is applied the moment the bill appears. Deleting a payment or
cancelling a bill rebuilds the whole customer's allocation, so the numbers can
never drift out of agreement.

## Bill checker

`Run bill checker` creates bills for active connections whose paid period has
ended. It reports a count per skip reason rather than failing quietly:

* `still_within_paid_period` — nothing owed yet
* `connection_not_active`
* `open_bill_already_exists`
* `no_plan_price` — connection has no plan and no charge amount
* `expiry_unknown_needs_status_sync` — we have never read this connection's expiry
  from the provider, so billing it would invent a due date. Run **Check status**
  on it first.

## Checks

```powershell
python scripts/smoke_test.py                 # money flow, on a throwaway database
python scripts/test_sweep.py                 # bulk status sweep: priority, retries, cancelling
python scripts/test_schedule.py              # the nightly scheduler fires once a day
python scripts/check_pages.py --port 8800    # every page and API endpoint, needs the app running
```

The first three use their own scratch database and force simulate mode, so they never
contact a provider portal.

## API

Same operations as the UI, for driving from the Telegram bot or a script.
Authenticate by POSTing `password` to `/login` and keeping the cookie.

```
GET  /api/stats
GET  /api/customers?q=&view=due|expiring|expired
GET  /api/customers/{id}
GET  /api/lookup?q=<STB / Railtel id / VC / phone>
POST /api/customers/{id}/payments   {"amount": 310, "connection_id": 1, "renew": true}
POST /api/connections/{id}/jobs     {"action": "renew"}
POST /api/jobs/{id}/confirm | /cancel | /retry
POST /api/bills/run-checker
```

Pass `"auto_confirm": true` to skip the approval step.

## Provider actions

| Action | Railtel | Hathway | vk_agent function |
| --- | --- | --- | --- |
| Renew | yes | yes | `check_railtel_renew_subscriber` / `check_hathway_renew_stb` |
| Check status | yes | yes | `check_railtel_portal` / `check_hathway_portal` |
| Clear session | yes | — | `check_clear_customer_session` |
| Retrack | — | yes | `check_hathway_retrack_stb` |
| Temporary deactivate | — | yes | `check_hathway_temp_deactivate` |
| Reactivate | — | yes | `check_hathway_temp_activate` |
| Remove pack + terminate | — | yes | `check_hathway_remove_pack_and_terminate` |
| Refresh wallet & counts | yes | yes | `check_railtel_billing_dashboard_kpis` / `check_hathway_dashboard_stats` |

### Retries only when a retry could help

A failed job retries once after `VK_PLATFORM_JOB_RETRY_MINUTES`, then stops and waits
for you — but only for failures that might succeed next time: login and CAPTCHA
failures, timeouts, network errors, and a Railtel wallet that was short (top it up and
retry). Answers that will read the same however often you ask — `STB is terminated`,
subscriber not found, an id that does not fit the provider — end the job immediately,
because each retry costs a full portal login and a CAPTCHA solve.

When a status check comes back `STB is terminated`, the connection is marked
`terminated` here too, so the bill checker stops charging for a box the provider has
already removed.

Railtel top-ups that fail because the partner wallet is short say so explicitly
instead of reporting a generic error.

The two destructive actions (terminate, deactivate) ask for a browser confirmation
before they are even queued, and every action still waits for approval on the jobs
page before a portal is opened.

### Provider ids are validated before anything runs

A Hathway set-top box is `N` + 11 digits (or `T` + 12 for a viewing card); a Railtel
login looks like `ka.username`. Before a job is queued the id is checked against its
provider's format, because the Bix export files a few broadband logins and
placeholders such as `NOBOX1` in the set-top box column, and driving the wrong portal
burns a real login attempt. Connections that fail the check keep billing normally but
show why their portal buttons are hidden, and they are all listed on **Providers**.

### Keeping expiry dates fresh

Billing needs to know when each connection actually expires, and only the provider knows.
Three ways to ask, all going through the same queue:

* **One connection** — Check status on the customer page.
* **One customer** — *Check all with provider* queues a check for every connection they
  have, across both providers, skipping any already in the queue.
* **Everything** — *Bulk status check* on the Providers page. Pick one provider or both,
  and a staleness threshold so it only asks about connections not synced in N days.
  It shows live progress, how many came back terminated, and an estimated time left.

Set a daily hour on the Providers page to have the sweep run by itself. Pick a quiet
hour: each check is a real login and takes roughly 25 seconds, so a full pass over 600
boxes is about four hours.

Sweeps are deliberately the lowest priority in the queue. A renewal you take at the
counter, or anything you click, overtakes every queued sweep check — so a nightly sweep
of 600 connections never makes a paying customer wait. A sweep skips terminated
connections and ids no portal could look up, will not start while another is running,
and closes itself when the last check lands.

### Who is online on Railtel

**Providers → Railtel online** is the same list the portal shows after you open
`https://ka.railwire.co.in/anpcntl` and click **Click Here** on the Online
subscribers tile (that lands on `https://services.railwire.co.in/dash.php`).
Refresh reads username, MAC, IP, session start, duration and usage in one login,
matches each username to a customer here, and updates **Active since** on those
connections. Usernames the platform has not imported are listed separately so you
can see boxes that are live on Railtel but missing from billing.

### The two portals report the same things differently

Worth knowing, because both cost us a bug:

| | Railtel / Railwire | Hathway |
| --- | --- | --- |
| Expiry format | `23/09/26 11:59:59 PM` | `12-OCT-26` |
| Session | `Active since 09/09/26 12:10:08 PM` | not reported |
| `mac` field means | the customer router's network MAC | the viewing card, `T` + 12 digits |

Neither expiry shape parses as a plain date, and the shared `mac` name hides two
unrelated values — a Railtel MAC must never be stored as a card number. `money.parse_date`
handles both date shapes, and `providers.card_number_from` only accepts a `mac` that
actually looks like a Hathway card.

If a parsing bug ever loses a value again, `scripts/repair_from_job_results.py` re-reads
the raw replies already saved in `upstream_jobs.result_json` and corrects what was stored,
without spending a single new portal login. It applies only the newest status reply per
connection, so it can never overwrite fresher data, and it is safe to re-run.

### Wallet balance

**Providers** shows each dealer account's wallet balance and box counts, read from the
provider's own dashboard by a job that belongs to the provider rather than to any
customer. Renewals fail when the wallet runs dry, so refresh it before a collection
round. Requesting a refresh while one is already pending returns the existing job
instead of stacking duplicates.

## Not built yet

PDF invoices, WhatsApp/SMS receipts, complaints,
inventory, and the Telegram side of the API. The receipt page prints from the
browser, which covers the common case.
