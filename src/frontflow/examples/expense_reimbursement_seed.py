"""
Seed fixtures for `expense_reimbursement`.

Routing notes: the form has an `@backend.branch` after `details`
that splits to `approval` for high amounts or skips it for low.
Scenarios fix the amount per branch to make the routing visible
in seeded data.
"""

SCENARIOS = [
    {
        "weight": 0.4,
        "advance_to": "summary",  # full path: low amount, no approval
        "values": {
            "claim": {
                "claimant": "Dana Reyes {i}",
                "category": "Meals",
                "amount": 42,
                "description": "Team lunch with two visiting partners.",
            },
            "details": {
                "category_confirm": "Meals",
                "receipt_id": "RCPT-2024-1014-A",
                "incurred_on": "2024-10-14",
                "is_duplicate": False,
            },
            "summary": {},
        },
    },
    {
        "weight": 0.3,
        "advance_to": "summary",  # high amount, approval branch
        "values": {
            "claim": {
                "claimant": "Alex Chen {i}",
                "category": "Travel",
                "amount": 2_450,
                "description": "Flight + hotel for SF customer meeting.",
            },
            "details": {
                "category_confirm": "Travel",
                "receipt_id": "RCPT-2024-1109-B",
                "incurred_on": "2024-11-09",
                "is_duplicate": False,
            },
            "approval": {
                "confirmed_category": "Travel",
                "approver": "Morgan Lee",
                "cost_center": "ENG-001",
            },
            "summary": {},
        },
    },
    {
        "weight": 0.15,
        "advance_to": "details",  # left mid-flow
        "values": {
            "claim": {
                "claimant": "Jordan Patel {i}",
                "category": "Equipment",
                "amount": 1_200,
                "description": "Standing desk + monitor arm.",
            },
            "details": {
                "category_confirm": "Equipment",
                "receipt_id": "RCPT-2024-1102-C",
                "incurred_on": "2024-11-02",
                "is_duplicate": False,
            },
        },
    },
    {
        "weight": 0.1,
        "advance_to": "claim",  # only claim submitted
        "values": {
            "claim": {
                "claimant": "Sam Rivera {i}",
                "category": "Software",
                "amount": 89,
                "description": "Annual license for design tool.",
            },
        },
    },
    {
        "weight": 0.05,
        "advance_to": None,
        "values": {},
    },
]
