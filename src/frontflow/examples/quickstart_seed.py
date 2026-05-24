"""
Seed fixtures for the `quickstart` example.

A list of scenarios the seeder draws from to populate the database
with realistic submissions. Each scenario carries a weight (relative
proportion in the seed distribution) and the values to submit at
each node it touches.

Used by `frontflow example seed quickstart`. If absent, the seeder
falls back to type-based auto-fill.
"""

SCENARIOS = [
    {
        "weight": 0.6,
        "advance_to": "feedback",  # full advance (last + only node)
        "values": {
            "feedback": {
                "name": "Dana Reyes",
                "likelihood": "Very likely",
                "comment": "Loving the new dashboard.",
            },
        },
    },
    {
        "weight": 0.25,
        "advance_to": "feedback",
        "values": {
            "feedback": {
                "name": "Alex Chen",
                "likelihood": "Somewhat",
                "comment": "",
            },
        },
    },
    {
        "weight": 0.1,
        "advance_to": "feedback",
        "values": {
            "feedback": {
                "name": "Jordan Patel",
                "likelihood": "Not at all",
                "comment": "Confusing onboarding flow.",
            },
        },
    },
    {
        "weight": 0.05,
        "advance_to": None,  # leave on landing — in-flight
        "values": {},
    },
]
