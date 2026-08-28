# Two ways of reading a page ship in this image.
#
# Almost every shop is read over plain HTTP by parsing the structured product
# metadata it already publishes. A few — price-comparison sites in particular —
# answer a plain request with a JavaScript challenge instead of the page, so
# real Chromium is here too, used only by sources marked `render: browser`.
# That is most of the image's size; the tracker still starts it only when a
# watchlist actually asks for it.
FROM python:3.11-slim

# tini reaps zombies and forwards signals, so `docker stop` actually stops the
# scheduler rather than waiting out the timeout.
# git is here because the UI can commit and push your config if you want that.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Chromium goes somewhere world-readable rather than into root's cache, because
# the container runs as uid 568 and could not read it there. HOME has to be
# writable too, and /config is the bind mount, so it is.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    HOME=/config

WORKDIR /src

# Dependencies first, so editing the source doesn't re-resolve the whole lock.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev --extra browser

COPY src/ ./src/
RUN uv sync --frozen --no-dev --extra browser

# --with-deps pulls the system libraries Chromium needs; matching them by hand
# is a moving target that Playwright already tracks.
RUN playwright install --with-deps chromium \
 && chmod -R a+rX /opt/pw-browsers \
 && rm -rf /var/lib/apt/lists/*

# The app reads watchlist.yaml and writes data/ relative to the working
# directory, so /config is where your bind mount goes.
WORKDIR /config

EXPOSE 8420
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["pricetracker", "schedule", "--at", "08:00"]
