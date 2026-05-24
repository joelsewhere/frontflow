"""
Seed fixtures for the `multi_backend_pipeline` example.

Used by `frontflow example seed multi_backend_pipeline`. Mixed
scenarios — some run all the way through the mock Airflow, some
stop at the process step (so the analytics show in-flight
operators), some fail at intake.
"""

SCENARIOS = [
    {
        "weight": 0.5,
        "advance_to": "process",  # runs through the whole pipeline
        "values": {
            "intake": {
                "job_name": "nightly-export",
                "priority": 5,
                "region": "us-east",
            },
            "process": {
                "comment": "Routine nightly run.",
                "tags": "ops, nightly",
            },
        },
    },
    {
        "weight": 0.2,
        "advance_to": "process",
        "values": {
            "intake": {
                "job_name": "ad-hoc-backfill",
                "priority": 8,
                "region": "us-west",
            },
            "process": {
                "comment": "Backfill triggered by support ticket #4421.",
                "tags": "backfill, support",
            },
        },
    },
    {
        "weight": 0.15,
        "advance_to": "intake",  # stops at process — DAG in-flight
        "values": {
            "intake": {
                "job_name": "eu-priority-1",
                "priority": 1,
                "region": "eu-west",
            },
        },
    },
    {
        "weight": 0.1,
        "advance_to": "intake",
        "values": {
            "intake": {
                "job_name": "weekly-rollup",
                "priority": 3,
                "region": "us-east",
            },
        },
    },
    {
        "weight": 0.05,
        "advance_to": None,  # never even submitted intake — bounced
        "values": {},
    },
]
