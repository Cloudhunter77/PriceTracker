# Running PriceTracker on TrueNAS SCALE

The TrueNAS host is immutable — `apt` is disabled and anything written to
`/usr/local/bin` disappears on the next system update — so this runs as a
container, not as a host install. The image is built by GitHub Actions and
pulled from GitHub Container Registry, so there is nothing to compile on the NAS.

Two containers from one image: a **scheduler** that runs the check once a day,
and the **web UI**. Both mount the same dataset, which holds your config and all
recorded data. The image holds only code, so updating it never touches your data.

> Every command below is run as `truenas_admin`, which is not root. Anything
> touching `/mnt`, ZFS or Docker needs `sudo`. `zfs` also lives in `/usr/sbin`,
> which is not on a non-root PATH — `sudo zfs` finds it, plain `zfs` does not.

## Why here and not GitHub Actions

The Actions workflow ran for nine days and returned `HTTP 403` from every shop.
Retailers fingerprint the TLS and HTTP/2 handshake, not just the User-Agent, and
GitHub's runners sit in datacenter IP ranges that get blocked wholesale. Your
server's own connection is not blocked — verified against the real shops before
any of this was built.

## 1. Create the dataset

Find your pool name first. It is whatever is mounted under `/mnt`:

```bash
ls /mnt
```

> Pool names can contain surprising characters — one real pool here has a
> **trailing space** in its name. Quote every path you type: `"/mnt/MY POOL /..."`.

Then create the dataset **in the web UI**: **Datasets → select your pool → Add
Dataset**, name it `pricetracker`.

### Then check the permissions — do not assume them

The containers run as uid/gid `568` (`apps`). Whether the new dataset is usable
by that account depends on the pool's ACL type: on an **NFSv4 ACL** pool the UI
adds an entry for the Apps group automatically, but on a pool using **classic
Unix permissions** the dataset arrives owned by `root:root` with no access for
`apps` at all. This has been seen in practice, so verify rather than trust it:

```bash
ls -ld /mnt/POOL/pricetracker        # want: drwxrwxr-x ... apps apps
```

If it says `root root`, fix it:

```bash
sudo chown -R 568:568 /mnt/POOL/pricetracker
sudo chmod 775 /mnt/POOL/pricetracker
```

Getting this wrong shows up later as the container being unable to write price
history, which is much harder to recognise than a permissions error.

<details>
<summary>Creating the dataset from the command line instead</summary>

```bash
sudo zfs create -p POOL/pricetracker
sudo chown -R 568:568 /mnt/POOL/pricetracker
sudo chmod 775 /mnt/POOL/pricetracker
```

`zfs` lives in `/usr/sbin`, so it needs `sudo` to be found at all.
</details>

## 2. Put your config in it

```bash
cd /mnt/POOL/pricetracker
sudo curl -sSLO https://raw.githubusercontent.com/Cloudhunter77/PriceTracker/claude/shopping-price-tracker-s3lu02/watchlist.yaml
sudo curl -sSLO https://raw.githubusercontent.com/Cloudhunter77/PriceTracker/claude/shopping-price-tracker-s3lu02/events.yaml
sudo chown 568:568 *.yaml
```

## 3. Check it works before scheduling anything

```bash
sudo docker run --rm \
  -v /mnt/POOL/pricetracker:/config \
  -e TZ=Europe/Budapest \
  ghcr.io/cloudhunter77/pricetracker:latest \
  pricetracker daily --dry-run
```

`--dry-run` reads real prices but records nothing and sends no email. You should
see a price from each shop.

If a shop errors, probe it the same way to see what the page actually offers:

```bash
sudo docker run --rm ghcr.io/cloudhunter77/pricetracker:latest \
  pricetracker test-url "https://www.example.hu/some-product"
```

## 4. Install as a TrueNAS app

**Apps → Discover → ⋮ → Install via YAML**, and paste
[`docker-compose.yaml`](../docker-compose.yaml) with `/mnt/POOL/pricetracker`
changed to your dataset's real path.

Set `TZ` in the YAML. Without it the container runs on UTC and `08:00` is the
wrong 08:00:

```yaml
TZ: Europe/Budapest
```

## Email is optional

TrueNAS has **no separate secrets field for apps installed via YAML** — a known,
currently unimplemented feature. The only place `GMAIL_USER` and
`GMAIL_APP_PASSWORD` can go is the same YAML box, which TrueNAS stores in plain
text. So there are two honest options:

**Leave email off.** This is fully supported. When no credentials are present the
digest is printed instead of sent, so it appears in
`sudo docker logs pricetracker-scheduler`, and the web UI shows every item below
its target. Nothing is lost or skipped — you just read your alerts rather than
receive them. Add credentials whenever you like.

**Accept plaintext.** App → Application Info → Edit, and replace
`${GMAIL_USER:-}` / `${GMAIL_APP_PASSWORD:-}` with real values. Use a Google
**app password**, never your account password, and revoke it from your Google
account if you ever change your mind — it grants mail-sending access to whoever
can read that YAML.

## 5. Watch the first real run

```bash
sudo docker logs -f pricetracker-scheduler
```

The scheduler logs when it starts, how long until the next run, and the outcome
of each one. To make it run immediately instead of waiting for 08:00, append
`--run-now` to its command in the app config and restart it.

## The UI and the internet

`http://<nas-ip>:8420` on your LAN.

**The UI has no authentication.** It can edit your watchlist, trigger outbound
requests from your server, and run git operations. Do not put it behind a
Cloudflare Tunnel hostname without an auth layer in front — Cloudflare Access
(free, email one-time-pin or Google login) takes a few minutes to configure and
is the difference between "my price tracker" and "a stranger's price tracker".

If you only ever use it from home, don't expose it at all.

## Updating

A new image is published on every push. To pick it up, **Restart** the app in
the TrueNAS UI — the compose file sets `pull_policy: always`, so a restart
fetches the current image. Your dataset is untouched.

## When something goes wrong

| Symptom | Cause |
| --- | --- |
| `zfs: command not found` | Not root. Use `sudo zfs`. |
| Permission denied writing `data/` | Dataset not owned by `568`. `sudo chown -R 568:568 /mnt/POOL/pricetracker` |
| The run happens at the wrong time | `TZ` is unset, so the container is on UTC |
| No email, no error | Expected when credentials are unset — the digest is printed to the log instead |
| `run failed` in the scheduler log | A real error. The log line above it says which stage |
| A shop reports 403 | That retailer blocks automated clients. Check with `test-url`, and use a shop that works — see the notes in `watchlist.yaml` |
