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

# Superset may READ frontflow's reporting views and NOTHING else.
#
# Connect and schema-usage only — no table grants here. frontflow's own
# startup grants SELECT on the reporting view it creates, and that is the
# only grant this role ever gets (see store._grant_reporting_view_to_superset_ro).
#
# This used to be `GRANT SELECT ON ALL TABLES` plus a default-privileges
# rule covering future tables. That handed Superset app_user.password_hash
# and app_session.token, and it broke row-level security: RLS predicates
# are looked up per *dataset*, so a query against a base table that has no
# dataset gets no predicate injected at all. Anyone with SQL Lab could read
# every row of every table by naming the table instead of the view.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${SUPERSET_RO_USER};
    GRANT USAGE ON SCHEMA public TO ${SUPERSET_RO_USER};
SQL

echo "init: created ${SUPERSET_METADATA_DB} and role ${SUPERSET_RO_USER}"
