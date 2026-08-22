# The pinned master build (see docker-compose.yml for why it is a commit and
# not a release tag) ships without database drivers, so Superset cannot reach
# its own metadata database out of the box.
FROM apache/superset:5ce52e5

USER root

# Install into the app's virtualenv explicitly. `python` on PATH resolves to
# /app/.venv/bin/python but `pip` resolves to the SYSTEM pip, so a plain
# `pip install` silently lands in the wrong interpreter and Superset still
# fails with ModuleNotFoundError. uv ships in the image and targets the venv.
RUN uv pip install --python /app/.venv/bin/python --no-cache psycopg2-binary==2.9.10 \
    && /app/.venv/bin/python -c "import psycopg2; print('psycopg2', psycopg2.__version__)"

USER superset
