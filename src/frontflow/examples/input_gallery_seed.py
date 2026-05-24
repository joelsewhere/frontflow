"""
Seed fixtures for `input_gallery`.

This form exists to showcase every input type, so a "seeded
submission" mostly means filling them with believable values. The
contact_method radio branches the follow-up; scenarios cover all
three branches so seeded data exercises the `@displays.branch`
routing.
"""

SCENARIOS = [
    {
        "weight": 0.35,
        "advance_to": "followup",  # email branch, full advance
        "values": {
            "basics": {
                "full_name": "Dana Reyes {i}",
                "contact_method": "Email",
                "email": "dana@example.com",
                "start_date": "2024-12-01",
                "agree": True,
            },
            "ranges": {
                "reporting_window": {
                    "start": "2024-01-01", "end": "2024-12-31",
                },
                "budget": {"min": 5_000, "max": 50_000},
                "regions": ["North America", "Europe"],
                "coverage": {
                    "Product": ["Q1", "Q2"],
                    "Marketing": ["Q3"],
                    "Support": ["Q4"],
                },
                "amenities": ["Wi-Fi", "Catering"],
                "notes": "Prefer afternoon sessions.",
                "notes_priority": "Medium",
            },
            "extras": {
                "work_email": "dana@example.com",
                "mobile": "+1-555-0101",
                "portfolio": "https://example.com/dana",
                "preferred_time": "14:00",
                "satisfaction": 4,
                "budget_estimate": 25_000,
                "s3_ready": "No",
            },
            "followup": {
                "confirm_name": "Dana Reyes",
                "primary_region": "North America",
                "email_current": True,
                "phone_current": False,
                "address_current": False,
            },
        },
    },
    {
        "weight": 0.25,
        "advance_to": "followup",  # phone branch
        "values": {
            "basics": {
                "full_name": "Alex Chen {i}",
                "contact_method": "Phone",
                "phone": "+1-555-0202",
                "ok_to_text": True,
                "start_date": "2024-11-15",
                "agree": True,
            },
            "ranges": {
                "reporting_window": {
                    "start": "2024-06-01", "end": "2024-12-01",
                },
                "budget": {"min": 1_000, "max": 10_000},
                "regions": ["Asia Pacific"],
                "coverage": {"Product": ["Q1"], "Marketing": ["Q1", "Q2"], "Support": ["Q3"]},
                "amenities": ["Parking", "Wi-Fi"],
                "notes": "",
                "notes_priority": "Low",
            },
            "extras": {
                "work_email": "alex@example.com",
                "preferred_time": "10:00",
                "satisfaction": 5,
                "budget_estimate": 5_000,
                "s3_ready": "No",
            },
            "followup": {
                "confirm_name": "Alex Chen",
                "primary_region": "Asia Pacific",
                "email_current": False,
                "phone_current": True,
                "address_current": False,
            },
        },
    },
    {
        "weight": 0.15,
        "advance_to": "followup",  # mail branch
        "values": {
            "basics": {
                "full_name": "Jordan Patel {i}",
                "contact_method": "Mail",
                "mailing_address": "123 Example St\nSpringfield, IL 62701",
                "start_date": "2024-10-20",
                "agree": True,
            },
            "ranges": {
                "reporting_window": {
                    "start": "2024-04-01", "end": "2024-10-31",
                },
                "budget": {"min": 500, "max": 5_000},
                "regions": ["Europe", "Africa"],
                "coverage": {"Product": ["Q1"], "Marketing": ["Q1", "Q2"], "Support": ["Q3"]},
                "amenities": [],
                "notes": "Mailing preferred.",
                "notes_priority": "High",
            },
            "extras": {
                "work_email": "jordan@example.com",
                "satisfaction": 3,
                "s3_ready": "No",
            },
            "followup": {
                "confirm_name": "Jordan Patel",
                "primary_region": "Europe",
                "email_current": False,
                "phone_current": False,
                "address_current": True,
            },
        },
    },
    {
        "weight": 0.15,
        "advance_to": "ranges",  # stopped after ranges
        "values": {
            "basics": {
                "full_name": "Morgan Park {i}",
                "contact_method": "Email",
                "email": "morgan@example.com",
                "start_date": "2024-11-01",
                "agree": True,
            },
            "ranges": {
                "reporting_window": {
                    "start": "2024-01-01", "end": "2024-06-30",
                },
                "budget": {"min": 2_000, "max": 20_000},
                "regions": ["South America"],
                "coverage": {"Product": ["Q1"], "Marketing": ["Q1", "Q2"], "Support": ["Q3"]},
                "amenities": ["Wi-Fi"],
                "notes": "",
                "notes_priority": "Low",
            },
        },
    },
    {
        "weight": 0.1,
        "advance_to": "basics",  # only basics
        "values": {
            "basics": {
                "full_name": "Sam Rivera {i}",
                "contact_method": "Email",
                "email": "sam@example.com",
                "start_date": "2024-12-10",
                "agree": True,
            },
        },
    },
]
