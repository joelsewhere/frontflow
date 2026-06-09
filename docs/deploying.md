# Deploying frontflow

Production deployment guidance. Three platform recipes (Heroku,
Docker, generic VPS), preceded by what every production deploy needs
regardless of platform.

If you're trying frontflow locally, see [`quickstart.md`](quickstart.md)
instead — the defaults are designed for zero-setup local use.

## What every production deploy needs

Three things differ from the local quickstart:

1. **A real database.** The default SQLite at `~/.frontflow/forms.db`
   works for one process on persistent disk; it won't work for
   anything else.
2. **An explicit `FRONTFLOW_SECRET_KEY`.** The auto-generated key
   writes to the filesystem — fine locally, catastrophic in any
   environment where the filesystem is ephemeral or where multiple
   instances need to share encryption.
3. **A reachable bind address.** Local defaults to `127.0.0.1`;
   production needs `0.0.0.0` (or whatever the platform expects).

## Configuration reference

All configuration is via environment variables. There's no config
file — `--env-file` is just a convenience that loads a `.env` into
the environment before the CLI runs. Anything that sets the
variables before the process starts works equivalently: platform
config UIs (Heroku, Render, Fly), Docker `-e`, Kubernetes env,
systemd `Environment=`.

| Variable | Required? | What it does |
|---|---|---|
| `DATABASE_URL` | Required for production | SQLAlchemy URL for the database. Postgres looks like `postgresql+psycopg://user:pass@host:5432/frontflow`. If unset, falls back to SQLite at `DB_PATH`. |
| `DB_PATH` | Optional | Path to a SQLite file. Ignored when `DATABASE_URL` is set. |
| `FRONTFLOW_HOME` | Optional | Override the default data directory (`~/.frontflow`). Affects where SQLite, the auto-generated key, and other local state live. |
| `FRONTFLOW_SECRET_KEY` | **Required for production** | A Fernet key used to encrypt credentials in the connection store. Generate with `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`. |
| `WORKFLOW_SOURCE` | Optional | Where to find workflow files. A directory path, or an `s3://bucket/prefix` URI. Falls back to the positional argument of `frontflow serve`, or the current directory. |

## Database

Production needs Postgres. The default SQLite is single-process,
single-machine, single-disk — anything beyond a personal install
needs more.

Install the Postgres extra:

```bash
pip install 'frontflow[postgres]'
```

This brings in `psycopg[binary]>=3.1`. Set `DATABASE_URL` to a
`postgresql+psycopg://...` URL:

```bash
export DATABASE_URL="postgresql+psycopg://frontflow:password@db.example.com:5432/frontflow"
```

The first `frontflow serve` against a fresh Postgres database
auto-creates the schema. There's no separate migration step today
(see "Caveats" below).

### Postgres URL schemes

frontflow rewrites two ambiguous Postgres URL prefixes at startup so
the same `DATABASE_URL` works on every platform:

- `postgres://...` → `postgresql+psycopg://...` (the Heroku/Render shape)
- `postgresql://...` → `postgresql+psycopg://...` (no explicit driver)

If you set `DATABASE_URL` to a URL with an explicit driver
(`postgresql+psycopg://`, `postgresql+asyncpg://`, `postgresql+pg8000://`),
frontflow leaves it untouched. The rewrite only fires for the two
ambiguous prefixes.

This means you can copy-paste whatever your platform provisions —
Heroku's `postgres://` legacy form included — and it works without
manual fixup.

## The encryption key

`FRONTFLOW_SECRET_KEY` encrypts credentials in the connection store
(API keys, Airflow auth, S3 credentials). Without it, frontflow
auto-generates one at startup and writes it to
`~/.frontflow/secret.key`. That fallback is for local development
only.

In production, generate one and set it explicitly:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Then set the output as `FRONTFLOW_SECRET_KEY`.

Two things to know:

1. **If the key is lost, every stored credential is unrecoverable.**
   Treat it like a database password. Store it in your secret
   manager. Rotate carefully — rotating without re-encrypting
   existing credentials orphans them.
2. **All instances must share the same key.** If you run multiple
   frontflow processes against the same database (load balancer,
   multiple dynos, Kubernetes replicas), every process needs the
   same `FRONTFLOW_SECRET_KEY` to decrypt credentials another
   instance encrypted.

## Workflow source

Workflow `.py` files have to be reachable from the process at
startup. Two patterns:

**Committed to the repo.** Put your workflows under `./forms/` (or
wherever) in your application's repo. `frontflow serve ./forms`
finds them. Simplest, version-controlled, gets reviewed with the
rest of your code.

**Loaded from S3.** Set `WORKFLOW_SOURCE=s3://bucket/prefix` and
install the S3 extra:

```bash
pip install 'frontflow[s3]'
```

The process needs AWS credentials reachable via the usual boto3
chain (env vars, instance profile, etc.). Useful when your workflow
authors aren't the same people who deploy the app, or when you want
to update workflows without redeploying.

## Server binding

frontflow uses uvicorn. The `serve` command takes `--host` and
`--port`:

```bash
frontflow serve ./forms --host 0.0.0.0 --port 8000
```

`0.0.0.0` is required to accept traffic from outside the host — the
local default of `127.0.0.1` is loopback-only.

If you're behind a reverse proxy (nginx, Caddy, ALB, Heroku router),
the proxy terminates TLS; uvicorn serves plain HTTP. The proxy
should forward `X-Forwarded-For` and `X-Forwarded-Proto`; uvicorn's
`--proxy-headers` flag (on by default in production-grade setups)
makes it honor them.

## Platform recipes

### Heroku

A working Heroku deployment needs a `Procfile`, a `requirements.txt`,
the Postgres add-on, and three config vars.

```bash
# 1. Create the app
heroku create my-frontflow-app

# 2. Add Postgres. Heroku auto-sets DATABASE_URL to the legacy
#    `postgres://...` shape; frontflow normalizes it at startup so
#    you don't need to touch it.
heroku addons:create heroku-postgresql:essential-0

# 3. Set the encryption key
heroku config:set FRONTFLOW_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

# 4. Deploy
git push heroku main
```

**`Procfile`:**

```
web: frontflow serve ./forms --host 0.0.0.0 --port $PORT
```

**`requirements.txt`:**

```
frontflow[postgres]
```

(Add `[s3]` too if you're loading workflows from S3.)

**Notes:**

- The dyno filesystem is ephemeral, so `DB_PATH` and the
  auto-generated key would be wiped on every restart. Postgres +
  `FRONTFLOW_SECRET_KEY` make that fine.
- Heroku scales by dyno count. If you scale past one dyno
  (`heroku ps:scale web=2`), all dynos share the same config vars,
  so the shared key + shared database work without further setup.
- Heroku passes `PORT` in the environment; the Procfile binds to it.

### Docker

A minimal Dockerfile:

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY forms/ ./forms/

EXPOSE 8000
CMD ["frontflow", "serve", "./forms", "--host", "0.0.0.0", "--port", "8000"]
```

**`requirements.txt`:**

```
frontflow[postgres]
```

Build and run:

```bash
docker build -t frontflow .
docker run \
    -p 8000:8000 \
    -e DATABASE_URL="postgresql+psycopg://..." \
    -e FRONTFLOW_SECRET_KEY="..." \
    frontflow
```

Or with `docker-compose.yml` for Postgres alongside:

```yaml
services:
  web:
    build: .
    ports: ["8000:8000"]
    environment:
      DATABASE_URL: postgresql+psycopg://frontflow:password@db:5432/frontflow
      FRONTFLOW_SECRET_KEY: ${FRONTFLOW_SECRET_KEY}
    depends_on: [db]
  db:
    image: postgres:16
    environment:
      POSTGRES_DB: frontflow
      POSTGRES_USER: frontflow
      POSTGRES_PASSWORD: password
    volumes:
      - frontflow-db:/var/lib/postgresql/data
volumes:
  frontflow-db:
```

Set `FRONTFLOW_SECRET_KEY` in your shell or a `.env` file Docker
Compose reads — never commit it.

### Generic VPS with systemd

Put the app in a directory, install into a venv, run under systemd.

```bash
# On the server
adduser --system --group frontflow
sudo -u frontflow git clone https://github.com/you/your-forms.git /srv/frontflow
cd /srv/frontflow
sudo -u frontflow python3 -m venv .venv
sudo -u frontflow .venv/bin/pip install 'frontflow[postgres]'
```

**`/etc/systemd/system/frontflow.service`:**

```ini
[Unit]
Description=frontflow
After=network.target postgresql.service

[Service]
Type=simple
User=frontflow
WorkingDirectory=/srv/frontflow
EnvironmentFile=/etc/frontflow/env
ExecStart=/srv/frontflow/.venv/bin/frontflow serve ./forms --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

**`/etc/frontflow/env`** (mode `0600`, owned by `frontflow`):

```
DATABASE_URL=postgresql+psycopg://frontflow:password@localhost:5432/frontflow
FRONTFLOW_SECRET_KEY=...
```

Put nginx (or Caddy) in front to terminate TLS and proxy to
`127.0.0.1:8000`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now frontflow
sudo journalctl -u frontflow -f
```

## Initial admin user

Whichever platform: after the first deploy, create an admin user so
you can reach the admin UI.

```bash
# Heroku
heroku run frontflow create-admin --username admin --password admin

# Docker
docker exec -it <container> frontflow create-admin --username admin --password admin

# systemd
sudo -u frontflow /srv/frontflow/.venv/bin/frontflow create-admin \
    --username admin --password admin
```

The command reads the same environment as `serve`, so it writes to
the same database.

## Multi-instance considerations

frontflow's state lives in the database, not in process memory, so
horizontal scaling is straightforward: all instances point at the
same Postgres + same `FRONTFLOW_SECRET_KEY`, and they can share a
load balancer with no sticky-session requirement.

That said:

- The current build is single-instance-tested. Multi-instance is
  expected to work but hasn't been exercised end-to-end. If you
  scale past one and see strange behavior, that's a bug worth
  reporting.
- `runtime._submissions_lock` serializes mutations to in-memory
  caches *within* a process; cross-process consistency relies on the
  database's UNIQUE constraints and the upsert pattern in
  `sync_submission`. Mostly safe, but two instances racing to advance
  the same submission could theoretically produce ordering surprises
  in the event log.
- The auto-generated key fallback is per-process and per-filesystem.
  If you forget to set `FRONTFLOW_SECRET_KEY` explicitly on a
  multi-instance deploy, each instance generates its own key and
  credentials encrypted by one become undecryptable by the others.
  Always set the key explicitly in production.

## Caveats

This guide is **documentation, not tested infrastructure**. Specific
points worth knowing:

- **No CI coverage against Postgres yet.** The test suite runs against
  SQLite. The code paths for non-SQLite are written (engine
  configuration branches on `_is_sqlite`), but a Postgres deployment
  hasn't been exercised end-to-end in the project's CI.
- **No migration tool.** The schema is auto-created at first run via
  `Base.metadata.create_all`. There's no `alembic` setup, so schema
  changes between frontflow versions could be a footgun on upgrade.
  Take a database backup before upgrading frontflow versions.
- **No published Docker image.** The Dockerfile above is an example;
  there's no official `frontflow/frontflow:latest` on Docker Hub.
- **Heroku-specific recipe is unverified.** The steps follow Heroku's
  standard patterns and should work, but I have not deployed
  frontflow to Heroku end-to-end myself.
- **TLS is the proxy's job.** uvicorn can serve TLS directly with
  `--ssl-keyfile` / `--ssl-certfile`, but every production recipe
  above terminates TLS at the reverse proxy. Cert rotation, OCSP
  stapling, modern cipher suites — all of that is easier in nginx /
  Caddy / a managed load balancer than in uvicorn.
- **Backup strategy is on you.** Whatever runs your Postgres needs
  its own backup story — point-in-time recovery, daily snapshots,
  whatever fits your risk model. frontflow has no built-in export
  loop for disaster recovery.

## Where to go next

- [`quickstart.md`](quickstart.md) — local install (start here if
  you haven't tried frontflow yet)
- [`authoring-a-form.md`](authoring-a-form.md) — write a form
- [`what-is-frontflow.md`](what-is-frontflow.md) — what frontflow is
