#!/bin/bash
# Runs once, on first initialisation of the Postgres data volume.
#
# Creates the Superset metadata database and the read-only role Superset
# uses to read frontflow's own data. frontflow's database itself is
# created by POSTGRES_DB; this only adds what Superset needs.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-SQL
    CREATE DATABASE ${SUPERSET_METADATA_DB};
    CREATE ROLE ${SUPERSET_RO_USER} WITH LOGIN PASSWORD '${SUPERSET_RO_PASSWORD}';
SQL

# Superset may READ frontflow's data and must never write to it. The
# grants below cover what exists now; frontflow's own startup grants
# SELECT on the reporting view it creates (see store._ensure_reporting_views).
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${SUPERSET_RO_USER};
    GRANT USAGE ON SCHEMA public TO ${SUPERSET_RO_USER};
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO ${SUPERSET_RO_USER};
    -- Tables and views created later must also be readable.
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT ON TABLES TO ${SUPERSET_RO_USER};
SQL

echo "init: created ${SUPERSET_METADATA_DB} and role ${SUPERSET_RO_USER}"
