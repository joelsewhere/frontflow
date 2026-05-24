"""
Seed fixtures for `publish_article`.

The form trails into mock Airflow: the editor's HITL branch routes
to `published`, `changes_requested`, or `rejected`. Mock Airflow
needs time to walk states, so seeded submissions mostly stop at
`draft` and the Airflow chain progresses on its own when polled.

Scenarios cover article submissions across the three channel
options. The HITL response (which routes the form) isn't directly
seedable — it'd require driving mock Airflow's HITL response path,
which is more work than this feature deserves. Seeded submissions
that stop at `draft` will appear "in flight" forever as the mock
Airflow waits on a HITL response that never comes; that's accurate
for the analytics view (lots of pending DAGs is a real state).
"""

SCENARIOS = [
    {
        "weight": 0.4,
        "advance_to": "draft",  # triggers the mock Airflow chain
        "values": {
            "draft": {
                "headline": "Our Q3 roadmap, in five themes {i}",
                "body": (
                    "We spent the last quarter listening to customers and "
                    "rebuilding our backlog around what matters most to them.\n\n"
                    "Five themes emerged…"
                ),
                "channel": "Blog",
                "feature": True,
            },
        },
    },
    {
        "weight": 0.25,
        "advance_to": "draft",
        "values": {
            "draft": {
                "headline": "Weekly digest — October 14 {i}",
                "body": "Top stories from the team this week…",
                "channel": "Newsletter",
                "feature": False,
            },
        },
    },
    {
        "weight": 0.15,
        "advance_to": "draft",
        "values": {
            "draft": {
                "headline": "Announcing our Series B {i}",
                "body": (
                    "Today we are announcing a $40M Series B led by Sequoia, "
                    "with participation from existing investors…"
                ),
                "channel": "Press release",
                "feature": True,
            },
        },
    },
    {
        "weight": 0.15,
        "advance_to": "draft",
        "values": {
            "draft": {
                "headline": "How we reduced p99 latency by 60% {i}",
                "body": (
                    "Latency was our top customer complaint last quarter. "
                    "Here's what we did…"
                ),
                "channel": "Blog",
                "feature": False,
            },
        },
    },
    {
        "weight": 0.05,
        "advance_to": None,  # bounced
        "values": {},
    },
]
