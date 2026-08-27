# Small on purpose: the tracker reads structured metadata over plain HTTP, so
# there is no browser to ship. The image is code only — your watchlist, events
# config and price history live in a bind-mounted volume, so pulling a new image
# never touches your data.
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

WORKDIR /src

# Dependencies first, so editing the source doesn't re-resolve the whole lock.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
RUN uv sync --frozen --no-dev

# The app reads watchlist.yaml and writes data/ relative to the working
# directory, so /config is where your bind mount goes.
WORKDIR /config

EXPOSE 8420
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["pricetracker", "schedule", "--at", "08:00"]
