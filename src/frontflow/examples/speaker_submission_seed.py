"""
Seed fixtures for `speaker_submission`.

Three input nodes (`speaker`, `logistics`, `agreement`) followed by
a workflow-level `compile_submission` backend. Scenarios cover the
full path and several drop-off points so the analytics rates and
step-time charts have variety.

Note: the `availability` field is a CheckboxGrid (rows × columns) —
its value is a dict keyed by row, then column → bool. The grid is
required, so a partial scenario must include at least one cell.
"""

# Default day-by-day availability — rows are "Day 1/2/3", columns
# "Prefer / Can do / Avoid". Value shape is `{row: [col, ...]}`,
# one entry per checked column.
_AVAIL_DEFAULT = {
    "Day 1": ["Prefer"],
    "Day 2": ["Can do"],
    "Day 3": ["Can do"],
}


SCENARIOS = [
    {
        "weight": 0.4,
        "advance_to": "agreement",  # full path
        "values": {
            "speaker": {
                "full_name": "Dana Reyes {i}",
                "available_from": "2024-11-01",
                "years_experience": 8,
                "session_format": "Talk",
                "talk_length": "40 minutes",
                "room_setup": "Theater",
                "capacity": 200,
                "co_panelists": "None",
                "short_bio": "Engineering leader with 8 years across "
                             "platforms and infra.",
            },
            "logistics": {
                "attendee_estimate": {"min": 100, "max": 200},
                "topics": ["Engineering", "Leadership"],
                "equipment": ["Projector", "Microphone"],
                "availability": _AVAIL_DEFAULT,
                "notes": "Prefer afternoon slot.",
            },
            "agreement": {
                "code_of_conduct": True,
                "dietary": "Vegetarian",
                "signature": "Dana Reyes",
            },
        },
    },
    {
        "weight": 0.25,
        "advance_to": "agreement",
        "values": {
            "speaker": {
                "full_name": "Alex Chen {i}",
                "available_from": "2024-12-05",
                "years_experience": 4,
                "session_format": "Workshop",
                "talk_length": "60 minutes",
                "room_setup": "Hands-on lab",
                "capacity": 30,
                "co_panelists": "Sam Rivera",
                "short_bio": "PM with a focus on developer tools.",
            },
            "logistics": {
                "attendee_estimate": {"min": 20, "max": 40},
                "topics": ["Product", "Design"],
                "equipment": ["Projector", "Whiteboard"],
                "availability": _AVAIL_DEFAULT,
                "notes": "Needs power strips at each table.",
            },
            "agreement": {
                "code_of_conduct": True,
                "dietary": "",
                "signature": "Alex Chen",
            },
        },
    },
    {
        "weight": 0.15,
        "advance_to": "logistics",  # stopped before agreement
        "values": {
            "speaker": {
                "full_name": "Jordan Patel {i}",
                "available_from": "2024-11-20",
                "years_experience": 12,
                "session_format": "Panel",
                "talk_length": "60 minutes",
                "room_setup": "Panel staging",
                "capacity": 400,
                "co_panelists": "Morgan Lee, Sam Rivera, Priya Nair",
                "short_bio": "Founder. Three exits. Currently advisor.",
            },
            "logistics": {
                "attendee_estimate": {"min": 200, "max": 500},
                "topics": ["Leadership", "Research"],
                "equipment": ["Microphone"],
                "availability": _AVAIL_DEFAULT,
                "notes": "",
            },
        },
    },
    {
        "weight": 0.1,
        "advance_to": "speaker",  # only first node
        "values": {
            "speaker": {
                "full_name": "Morgan Park {i}",
                "available_from": "2024-10-15",
                "years_experience": 2,
                "session_format": "Lightning talk",
                "talk_length": "20 minutes",
                "room_setup": "Lightning stage",
                "capacity": 50,
                "co_panelists": "None",
                "short_bio": "",
            },
        },
    },
    {
        "weight": 0.1,
        "advance_to": None,
        "values": {},
    },
]
