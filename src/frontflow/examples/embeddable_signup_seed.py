"""Seed fixtures for the `embeddable_signup` example."""

SCENARIOS = [
    {
        "weight": 0.5,
        "advance_to": "thanks",
        "values": {
            "signup": {
                "email": "alex@example.com",
                "cadence": "Monthly",
            },
        },
    },
    {
        "weight": 0.25,
        "advance_to": "thanks",
        "values": {
            "signup": {
                "email": "jordan@example.com",
                "cadence": "Weekly",
            },
        },
    },
    {
        "weight": 0.15,
        "advance_to": "thanks",
        "values": {
            "signup": {
                "email": "sam@example.com",
                "cadence": "Major releases only",
            },
        },
    },
    {
        "weight": 0.1,
        "advance_to": None,  # closed the iframe before submitting
        "values": {},
    },
]
