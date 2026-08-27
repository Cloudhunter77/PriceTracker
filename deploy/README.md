# Running PriceTracker on TrueNAS SCALE

The TrueNAS host is immutable — `apt` is disabled and anything written to
`/usr/local/bin` disappears on the next system update — so this runs as a
container, not as a host install.

Two containers from one image: a **scheduler** that runs the check once a day,
and the **web UI**. Both mount the same dataset, which holds your config and all
recorded data. The image holds only code, so updating it never touches your data.

## Why here and not GitHub Actions

The Actions workflow ran for nine days and returned `HTTP 403` from every shop.
Retailers fingerprint the TLS and HTTP/2 handshake, not just the User-Agent, and
GitHub's runners sit in datacenter IP ranges that get blocked wholesale. Your
server's own connection is not blocked — verified against the real shops before
any of this was built.

## 1. Create the dataset

```bash
zfs create tank/apps/pricetracker          # adjust the pool name
chown -R 568:568 /mnt/tank/apps/pricetracker
```

`568` is TrueNAS's `apps` user, which is what the containers run as. If the
dataset is owned by root, the container cannot write price history and every run
will fail.

Copy `watchlist.yaml` and `events.yaml` into it:

```bash
cd /mnt/tank/apps/pricetracker
curl -sSLO https://raw.githubusercontent.com/Cloudhunter77/PriceTracker/claude/shopping-price-tracker-s3lu02/watchlist.yaml
curl -sSLO https://raw.githubusercontent.com/Cloudhunter77/PriceTracker/claude/shopping-price-tracker-s3lu02/events.yaml
chown 568:568 *.yaml
```

## 2. Build the image

The compose file builds from source, so clone the repo somewhere on the NAS:

```bash
git clone -b claude/shopping-price-tracker-s3lu02 \
  https://github.com/Cloudhunter77/PriceTracker.git /mnt/tank/apps/pricetracker-src
cd /mnt/tank/apps/pricetracker-src
docker build -t pricetracker:latest .
```

## 3. Check it works before scheduling anything

```bash
docker run --rm -v /mnt/tank/apps/pricetracker:/config -e TZ=Europe/Budapest \
  pricetracker:latest pricetracker daily --dry-run
```

`--dry-run` reads real prices but records nothing and sends no email. You should
see a price for each shop. If a shop errors, run `pricetracker test-url <url>`
the same way to see what the page actually offers.

## 4. Install as a TrueNAS app

**Apps → Discover → ⋮ → Install via YAML**, and paste `docker-compose.yaml`
from the repo with the volume path changed to your dataset.

Set the email credentials as environment variables in the app's config —
**not** inline in the YAML, which TrueNAS stores in plain text:

| Variable | Value |
| --- | --- |
| `GMAIL_USER` | your Gmail address |
| `GMAIL_APP_PASSWORD` | a Google **app password**, not your account password |
| `TZ` | `Europe/Budapest` — without it the container runs UTC and 08:00 is the wrong 08:00 |

## 5. Watch the first real run

```bash
docker logs -f pricetracker-scheduler
```

The scheduler logs when it starts, how long until the next run, and the outcome
of each one. To force a run immediately rather than waiting for 08:00, restart
it with `--run-now` appended to the command.

## The UI and the internet

`http://<nas-ip>:8420` on your LAN.

**The UI has no authentication.** It can edit your watchlist, trigger outbound
requests from your server, and run git operations. Do not put it behind a
Cloudflare Tunnel hostname without an auth layer in front — Cloudflare Access
(free, email one-time-pin or Google login) takes a few minutes to configure and
is the difference between "my price tracker" and "a stranger's price tracker".

If you only ever use it from home, don't expose it at all.

## Updating

```bash
cd /mnt/tank/apps/pricetracker-src && git pull
docker build -t pricetracker:latest .
# then Restart the app in the TrueNAS UI
```

Your dataset is untouched by this.
