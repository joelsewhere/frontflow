"""Seed fixtures for role_demo_expense."""

SCENARIOS = [
    {
        "weight": 0.5,
        "advance_to": "thanks",
        "values": {
            "request": {
                "amount": 250,
                "purpose": "Team offsite snacks and beverages.",
            },
            "approval": {
                "decision": "Approve",
                "notes": "Within budget.",
            },
        },
    },
    {
        "weight": 0.3,
        "advance_to": "thanks",
        "values": {
            "request": {
                "amount": 4200,
                "purpose": "Conference attendance + travel.",
            },
            "approval": {
                "decision": "Reject",
                "notes": "Above per-trip cap. Please re-scope.",
            },
        },
    },
    {
        "weight": 0.2,
        "advance_to": "approval",   # awaiting approver
        "values": {
            "request": {
                "amount": 1850,
                "purpose": "New laptop for new hire.",
            },
        },
    },
]
