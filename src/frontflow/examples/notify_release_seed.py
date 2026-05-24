"""
Seed fixtures for the `notify_release` example.

Realistic release announcements the seeder can submit to populate the
database. Used by `frontflow example seed notify_release`.
"""

SCENARIOS = [
    {
        "weight": 0.4,
        "advance_to": "done",
        "values": {
            "compose": {
                "version": "2.4.0",
                "summary": (
                    "Adds the Variables admin page and "
                    "`{{ variables.x }}` template support."
                ),
                "channel": "#releases",
            },
        },
    },
    {
        "weight": 0.3,
        "advance_to": "done",
        "values": {
            "compose": {
                "version": "2.3.5",
                "summary": "Patch — fixes a regression in the form scanner.",
                "channel": "#releases",
            },
        },
    },
    {
        "weight": 0.2,
        "advance_to": "done",
        "values": {
            "compose": {
                "version": "2.5.0-beta.1",
                "summary": "Beta — preview of the Listings v1 work.",
                "channel": "#beta-testers",
            },
        },
    },
    {
        "weight": 0.1,
        "advance_to": None,  # in-flight, never submitted
        "values": {},
    },
]
