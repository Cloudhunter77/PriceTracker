# PriceTracker

Watches the price of things you want to buy, across as many shops as you like,
and emails you when one actually drops to your number.

It runs itself once a day on GitHub Actions — nothing to install on your
machine, nothing to leave switched on.

```
Sony Alpha A7 IV váz
  1 299 900 Ft at alza.hu
  at or below your target of 1 500 000 Ft
  previous: 1 499 900 Ft (-13.3%)
```

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

### 2. Set up the alert email

Gmail won't accept your account password from a script — you need an **app
password**:

1. Turn on 2-Step Verification at [myaccount.google.com/security](https://myaccount.google.com/security)
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. Create one named `PriceTracker` and copy the 16-character code

Add both as repository secrets under **Settings → Secrets and variables →
Actions → New repository secret**:

| Secret | Value |
| --- | --- |
| `GMAIL_USER` | your Gmail address |
| `GMAIL_APP_PASSWORD` | the 16-character app password |

To send alerts somewhere other than your own inbox, add a repository *variable*
`ALERT_EMAIL_TO` (comma-separated for several addresses).

Not a Gmail user? Set `SMTP_HOST`, `SMTP_PORT`, and `SMTP_USE_SSL=false` for a
provider that uses STARTTLS.

### 3. Let it run

The [daily workflow](.github/workflows/daily-check.yml) runs at 07:15 UTC and
commits each day's prices back to `data/history.jsonl`. Trigger it by hand from
the **Actions** tab (**Run workflow**) to check it works — tick **dry run** to
check prices without recording or emailing anything.

## Running it locally

```bash
uv sync
uv run pricetracker check --dry-run     # check prices, change nothing
uv run pricetracker check               # check, record, and email
```

For a local email test, export the same variables first:

```bash
export GMAIL_USER=you@gmail.com GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
```

## Commands

| Command | What it does |
| --- | --- |
| `check` | Check every source once, record it, alert on drops |
| `add <url>` | Add a product URL to the watchlist |
| `list` | Show the watchlist and the latest price of each item |
| `history [item]` | Show recorded price history |
| `test-url <url>` | Probe one URL and show what each method finds |
| `where` | Show where the data files live |

Useful flags on `check`: `--dry-run` (change nothing), `--no-email` (record but
stay quiet), `--item "name"` (check just one thing), `--verbose`.

## When a shop doesn't work

Run `pricetracker test-url <url>`. If every method comes up empty, the shop
renders its price in JavaScript or blocked the request. Open the page in a
browser, right-click the price → Inspect, copy a CSS selector for that element,
and confirm it:

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

## Things worth knowing

**Amazon is not supported.** It blocks the datacenter IPs that GitHub Actions
runs on, and renders prices in JavaScript. Supporting it properly needs a
headless browser or a paid API, and would still break regularly.

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
| `data/debug/` | Pages that failed to parse (git-ignored) |

## Development

```bash
uv sync
uv run pytest
```

Tests run against saved HTML fixtures with no network access.
