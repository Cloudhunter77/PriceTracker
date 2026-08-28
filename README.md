# PriceTracker

Watches the price of things you want to buy across as many shops as you like,
keeps an eye on interesting events happening near you, and sends you one email a
day when there's something worth knowing.

It runs itself once a day in a container on your own server, and comes with a
web UI so you never have to touch a command line.

Deploying to TrueNAS SCALE? See [deploy/README.md](deploy/README.md) — the image
is published to `ghcr.io/cloudhunter77/pricetracker`, so there is nothing to build.

```
PRICE ALERTS

Sony Alpha A7 IV váz
  1 299 900 Ft at alza.hu
  at or below your target of 1 500 000 Ft
  previous: 1 499 900 Ft (-13.3%)

COMING UP NEARBY

Fri 4 Sep, 20:00 — Éjszakai Koncert a Dunán
  A38 Hajó · 2.9 km away
  4 500 Ft · Live music
```

## The web UI

```bash
uv run pricetracker ui
```

Opens at `http://127.0.0.1:8420`. Everything the CLI does, in a browser: add and
edit items, see price history charts, manage event sources, run a check on
demand.

The best bit is **Test this URL** on the add-item page — it fetches the page and
shows you what each extraction method found *before* you save, so you find out
immediately whether a shop is readable rather than tomorrow morning.

Because the daily run happens on GitHub, changes you make locally need pushing.
The UI pulls before it writes, and shows a **Push** button in the header whenever
there's something to send.

## How it reads prices

Rather than a hand-written scraper per shop, it reads the structured product
metadata shops already publish for Google Shopping. Four methods are tried in
order and the first hit wins:

| Method | What it reads |
| --- | --- |
| `selector` | A CSS/XPath override you put in the watchlist |
| `json-ld` | `schema.org` `Product`/`Offer` in a `<script type="ld+json">` block |
| `microdata` | `itemprop="price"` markup |
| `opengraph` | `og:price:amount` / `product:price:amount` meta tags |

Most real shops — Alza, eMAG, MediaMarkt, B&H, Best Buy — work through one of
the metadata methods with no per-site configuration. The selector is there for
the ones that don't.

## Price-comparison pages

One Árukereső page carries the whole Hungarian market for a product: every shop
selling it, with its price. Tracking that page is worth more than tracking a
dozen shops by hand, and it keeps working when a shop you never configured
undercuts everyone.

```yaml
sources:
  - url: https://www.arukereso.hu/fenykepezogep-c3128/sony/alpha-a6700-p1
    type: aggregator     # read as many shops, not one
    render: browser      # this site needs JavaScript run
```

`type: aggregator` turns one page into **one reading per shop it names**, so
each shop gets its own line in the history and its own chart series, and the
cheapest of them is what your target is compared against. If a comparison page
publishes no per-shop markup, the market low — the "513 120 Ft-tól" figure — is
recorded instead, which is the only number such a page actually promises.

`render: browser` runs the page in real Chromium instead of making a plain HTTP
request. Árukereső and Heureka answer a plain request with Cloudflare's "Just a
moment…" challenge, which is a page asking to be executed rather than a header
to get right. Chromium executes it and carries on.

Two things worth knowing about it:

- **It is opt-in per source.** A watchlist with no `render: browser` never
  starts a browser, and the daily run stays in milliseconds.
- **The browser profile persists** in `data/browser/`, so the clearance cookie
  from a solved challenge is reused. The challenge is solved roughly once, not
  every morning — faster for you and much less load on them.

Probe one before committing it:

```bash
pricetracker test-url <url> --render browser
```

In the UI this is the **Kind of page** dropdown when you add a shop.

**Where this stops.** Some sites reject the TLS handshake before any JavaScript
runs — Alza, MediaMarkt and B&H do, from this network — and no browser setting
reaches that. Running a site's own challenge is fair; forging fingerprints or
solving CAPTCHAs is a different activity and this tool does not do it. If a
page still shows the challenge after 30 seconds, the run says so and moves on.

## How it finds events

The same trick, pointed at `schema.org/Event` instead of `Product`. Two kinds of
source:

| Type | What it reads |
| --- | --- |
| `schemaorg` | Event markup on a listing or venue page |
| `ics` | An iCalendar feed — unambiguous, and the most reliable of the two |

**There is no event API to call for Budapest.** Eventbrite shut its public search
API in 2020, Songkick charges a license fee, Bandsintown is partnership-only,
Meetup needs a paid Pro plan, and Ticketmaster — the one free option — does not
cover Hungary. Scraping structured data is what's left.

That makes verifying a source the first thing to do, since whether a site is
readable depends entirely on what it publishes:

```bash
pricetracker events test-source https://port.hu/programok/budapest
```

or hit **Test this source** on the Events page. The sources shipped in
[`events.yaml`](events.yaml) are a **starting point, not a verified list** —
test each one, drop what comes back empty, and prefer a venue's `.ics` feed
wherever one exists.

An event is reported when it starts within `window_days`, sits within
`radius_km` of home, and matches one of your interest keywords. Matching folds
accents and case, so `Színház`, `szinhaz` and `SZÍNHÁZ` are one word. Venue
addresses are geocoded once via OpenStreetMap and cached permanently.

Each event is mentioned **once** — `data/events_seen.json` remembers what you've
already been told, the same way price alerts avoid repeating themselves.

## Setup

### 1. Add your items

Edit [`watchlist.yaml`](watchlist.yaml), or use the CLI:

```bash
pricetracker add https://www.alza.hu/sony-alpha-a7-iv-vaz-d6799672.htm \
  --name "Sony A7 IV" --target 900000 --currency HUF
```

Add a second shop for the same thing by reusing the name — the cheaper of the
two is what gets compared against your target:

```bash
pricetracker add https://www.emag.hu/... --name "Sony A7 IV" --target 900000
```

Then confirm the price can be read:

```bash
pricetracker test-url https://www.alza.hu/sony-alpha-a7-iv-vaz-d6799672.htm
```

### 2. Set up the alert email — or don't

**Email is optional.** With no credentials set, the digest is printed to stdout
instead of sent, so it shows up in the container log and the web UI still lists
everything below its target. Nothing is skipped; you read your alerts rather than
receive them.

To actually send mail, Gmail needs an **app password**, not your account password:

1. Turn on 2-Step Verification at [myaccount.google.com/security](https://myaccount.google.com/security)
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. Create one named `PriceTracker` and copy the 16-character code

Then set these in the environment wherever it runs:

| Variable | Value |
| --- | --- |
| `GMAIL_USER` | your Gmail address |
| `GMAIL_APP_PASSWORD` | the 16-character app password |
| `ALERT_EMAIL_TO` | optional; comma-separated, defaults to yourself |

Not a Gmail user? Set `SMTP_HOST`, `SMTP_PORT`, and `SMTP_USE_SSL=false` for a
provider that uses STARTTLS.

### 3. Set up events

Edit [`events.yaml`](events.yaml) or use the Events page: set your coordinates
and radius, list the keywords that interest you, and add sources. Test each
source before relying on it.

### 4. Let it run

`pricetracker schedule --at 08:00` runs the check once a day and keeps going —
prices and events in one pass, one digest. In the container that is the
`scheduler` service, and its output is `docker logs pricetracker-scheduler`.

It is deliberately not cron: cron in a container does not inherit the
environment, which is exactly how SMTP credentials go missing and alerts stop
with no visible error.

The [GitHub Actions workflow](.github/workflows/daily-check.yml) is still there
for manual runs, but its schedule is **disabled** — see below for why.

## Running it locally

```bash
uv sync
uv run pricetracker ui                  # the web UI — start here
uv run pricetracker daily --dry-run     # check everything, change nothing
uv run pricetracker daily               # check, record, and email
```

Tracking a comparison page needs Chromium, which the container image already
has. Locally, install it once:

```bash
uv sync --extra browser
uv run playwright install chromium
```

For a local email test, export the same variables first:

```bash
export GMAIL_USER=you@gmail.com GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
```

## Commands

| Command | What it does |
| --- | --- |
| `ui` | Start the web UI |
| `daily` | Check prices **and** events, send one email — what the workflow runs |
| `check` | Prices only |
| `add <url>` | Add a product URL to the watchlist |
| `list` | Show the watchlist and the latest price of each item |
| `history [item]` | Show recorded price history |
| `test-url <url>` | Probe one URL and show what each price method finds (`--render browser` for sites that need JavaScript) |
| `find <item>` | Search other shops for something you already track |
| `test-search <shop> <query>` | Probe one shop's search and show what it yields |
| `events check` | Scan event sources |
| `events list` | Show upcoming events already found |
| `events test-source <url>` | Probe one event source (`--type schemaorg\|ics`) |
| `where` | Show where the data files live |

Useful flags on `check` and `daily`: `--dry-run` (change nothing), `--no-email`
(record but stay quiet), `--item "name"` (check just one thing), `--verbose`.

## Finding the same product at other shops

Adding a second shop by hand means going and finding its URL. `find` does the
looking:

```bash
pricetracker find "Sony A6700 váz" --model ILCE-6700B
```

It searches the shops in [`shops.yaml`](shops.yaml), opens each result to read
its real price, and ranks what it found. In the web UI the same thing is the
**Find it at other shops** button on an item, with checkboxes.

**Nothing is added without you ticking it**, and that is the point. Searching
for a camera returns the body, the kit-lens bundle, a spare battery and a cage —
and a bundle mistaken for a body is a six-figure error in HUF. Matching is on
the manufacturer part number first (found in the URL slug, the page's `mpn`, or
the title), falling back to title similarity; anything advertising a kit, szett,
csomag or használt is demoted and never pre-ticked.

Once confirmed, a shop is an ordinary source. Your target price applies to
whichever shop is cheapest, so one target covers them all.

Whether a shop can be searched at all depends on its results page linking to
products in plain HTML. Check before relying on it:

```bash
pricetracker test-search emag.hu "A6700"
```

Only eMAG's pattern in `shops.yaml` is confirmed; the rest ship marked
`verified: false`. Fix a wrong `search` or `product_path` in that file — it
lives in your config volume, so no rebuild is needed.

## Buying in another currency

A shop pricing in euros is compared against a **euro target**, not a converted
one:

```yaml
- name: Sony A6700 váz
  currency: HUF
  target_price: 500000        # what a Hungarian shop has to beat
  targets:
    EUR: 1200                 # what a Slovak shop has to beat
  sources:
    - url: https://www.tripont.hu/...
    - url: https://obchod.sk/...
```

**No exchange rate is applied anywhere.** That is deliberate: with conversion, a
forint that weakened overnight could trip an alert on a price that never moved,
and a real drop could be masked by the rate going the other way. Two numbers you
control beat one number that drifts.

Each currency is judged, and remembered, separately — so a forint alert never
silences a euro one during its cooldown, and a euro drop is compared against euro
history rather than forint history.

**A currency with no target is an error, not a comparison.** If a page prices in
EUR and the item has no EUR target, the reading is recorded as an error saying so.
That is the rule that stops a €2 499 camera looking like a bargain against a
forint target; setting a target is how you opt a currency in.

Set the EUR target in the web UI on the item page, or in `watchlist.yaml`.

## When a shop doesn't work

Run `pricetracker test-url <url>`. If it reports a JavaScript challenge, retry
with `--render browser` — that is a different problem with a different fix, and
no selector will help. If every method simply comes up empty, the shop renders
its price in JavaScript. Open the page in a browser, right-click the price →
Inspect, copy a CSS selector for that element, and confirm it:

```bash
pricetracker test-url <url> --selector ".product-price .final"
```

Then put it in the watchlist:

```yaml
sources:
  - url: https://example.hu/thing
    selector: .product-price .final
```

Selectors break when a shop redesigns, so prefer a metadata method when one
works. If a source starts failing, the daily run records the error and attaches
the unparsed page to the workflow run as an artifact.

## If a shop returns 403

**Read this before trusting the daily run.** The first nine days of real runs
failed with `HTTP 403` from every shop, and the cause is where the check runs,
not how it parses.

Two things cause it, and they need different fixes:

1. **A bot-shaped User-Agent.** Fixed — the default is now a normal browser UA.
2. **GitHub Actions IP addresses.** Retailers and their bot-protection vendors
   block datacenter IP ranges wholesale, and Actions runners sit squarely in
   them. Nothing in this repo can talk a WAF out of that.

If 403s continue after the UA change, the second cause is the one biting, and
the fix is to run the check from your own network instead:

```bash
# Every day at 08:00, from your own machine and your own IP.
crontab -e
0 8 * * *  cd ~/PriceTracker && /usr/bin/env uv run pricetracker daily >> ~/pricetracker.log 2>&1
```

Set `GMAIL_USER` and `GMAIL_APP_PASSWORD` in that environment so it can still
email you. Everything else works identically — same files, same UI, same
behaviour. You can leave the GitHub workflow enabled as a backup or disable it
in the **Actions** tab.

Check which shops are actually working at any time:

```bash
uv run pricetracker history | tail -20
```

## Things worth knowing

**Amazon is not supported.** It blocks datacenter IPs and renders prices in
JavaScript. Supporting it properly needs a paid API, and would still break
regularly. Note that plenty of *other* shops block Actions runners too — see
"If a shop returns 403" above.

**You won't be spammed.** Once an item alerts, it stays quiet until the price
drops at least another 1% or `cooldown_days` pass. If the price recovers and
drops again later, that alerts as normal.

**A price that looks too good is held back.** A reading more than 70% below the
last known price is usually a mis-parse — an accessory, or a "from" price — so
it's recorded as `suspect` and only alerts if the next day's run confirms it.

**Out-of-stock prices don't trigger alerts**, since you can't act on them. Set
`alert_on_out_of_stock: true` if you'd rather see them.

**Currencies are never mixed.** If a page reports a currency that doesn't match
the watchlist, the reading is recorded as an error rather than compared.

**Online-only and cancelled events are skipped** — a webinar is not something
happening near you, and a cancelled gig is not worth an email.

**robots.txt** is checked and logged but not enforced, since many retailers
disallow all bots wholesale. At one request per product per day with an
identifying User-Agent this is negligible load; set `respect_robots: true` under
`defaults` to be strict.

## Your data

| File | What's in it |
| --- | --- |
| `watchlist.yaml` | What you're tracking |
| `data/history.jsonl` | One line per source per run — diffable in git, loads into pandas |
| `data/alert_state.json` | What you were last told, so it isn't repeated |
| `events.yaml` | Where you are, what interests you, where to look |
| `data/events.jsonl` | Every event found, append-only |
| `data/events_seen.json` | Events already mentioned, so they aren't repeated |
| `data/geocache.json` | Cached venue coordinates |
| `data/debug/` | Pages that failed to parse (git-ignored) |
| `data/browser/` | Chromium's profile, so a solved challenge is not re-solved daily (git-ignored) |
| `data/browser-probe/` | The same, for `test-url` and the UI's probe — a profile can't be shared between two processes (git-ignored) |

## Development

```bash
uv sync
uv run pytest
```

Tests run against saved HTML fixtures with no network access.
