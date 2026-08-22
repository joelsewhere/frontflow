#!/bin/bash
# One-shot Superset initialisation. Every step is idempotent, so
# `docker compose up` against an existing volume is a no-op.
set -euo pipefail

echo "==> Upgrading Superset metadata database"
superset db upgrade

echo "==> Creating admin user (ignored if it already exists)"
superset fab create-admin \
    --username "${SUPERSET_ADMIN_USERNAME}" \
    --firstname Admin \
    --lastname User \
    --email "${SUPERSET_ADMIN_EMAIL}" \
    --password "${SUPERSET_ADMIN_PASSWORD}" || true

echo "==> Initialising roles and permissions"
superset init

echo "==> Registering frontflow's database as a Superset connection"
# The name must match FRONTFLOW_SUPERSET_DATABASE (default "FrontFlow"),
# which is how provisioning finds the database to build datasets on.
superset set-database-uri \
    --database_name "${FRONTFLOW_SUPERSET_DATABASE:-FrontFlow}" \
    --uri "${FRONTFLOW_DB_RO_URI}" || \
    echo "    (set-database-uri unavailable; add the connection via the UI)"

echo "==> Superset bootstrap complete"
