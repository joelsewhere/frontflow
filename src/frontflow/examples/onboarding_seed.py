"""Seed fixtures for the `onboarding` example. Realistic new-account
setups the seeder can submit to populate the analytics views."""

SCENARIOS = [
    {
        "weight": 0.4,
        "advance_to": "done",
        "values": {
            "basics": {
                "name": "Dana Reyes",
                "email": "dana@example.com",
            },
            "preferences": {
                "theme": "Light",
                "notifications": True,
            },
            "confirm": {"display_name": "Dana"},
        },
    },
    {
        "weight": 0.3,
        "advance_to": "done",
        "values": {
            "basics": {
                "name": "Alex Chen",
                "email": "alex.chen@example.com",
            },
            "preferences": {
                "theme": "Dark",
                "notifications": False,
            },
            "confirm": {"display_name": "Alex Chen"},
        },
    },
    {
        "weight": 0.15,
        "advance_to": "preferences",  # left partway through
        "values": {
            "basics": {
                "name": "Jordan Patel",
                "email": "jordan.p@example.com",
            },
        },
    },
    {
        "weight": 0.1,
        "advance_to": "confirm",  # made it to confirm but didn't submit
        "values": {
            "basics": {
                "name": "Sam Park",
                "email": "sam.park@example.com",
            },
            "preferences": {
                "theme": "System default",
                "notifications": True,
            },
        },
    },
    {
        "weight": 0.05,
        "advance_to": None,  # bailed on the first screen
        "values": {},
    },
]
