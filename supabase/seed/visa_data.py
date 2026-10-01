"""Visa rules for Indian passport holders. INDICATIVE ONLY: rules, fees and processing times change often.
The bot tells users to confirm with the embassy; refresh this table from an official source before launch."""

DOCS_LABELS = {
    "passport": "Passport first page (photo page)",
    "photo": "Passport-size photo (white background)",
    "bank_statement": "Last 6 months bank statement",
    "itinerary": "Flight tickets / itinerary",
    "hotel_booking": "Hotel booking confirmation",
    "invitation": "Business invitation letter",
}

E_BASIC = ["passport", "photo", "itinerary"]
STICKER = ["passport", "photo", "bank_statement", "itinerary", "hotel_booking"]
BIZ_E = E_BASIC + ["invitation"]
BIZ_STICKER = STICKER + ["invitation"]

# (code, name, flag): {purpose: (type, fee_inr, processing_days, max_stay_days, docs, notes)}
RULES = {
    ("AE", "UAE", "🇦🇪"): {
        "tourist": ("e-visa", 6500, 4, 30, E_BASIC, "Single entry. Return ticket required."),
        "business": ("e-visa", 7500, 5, 30, BIZ_E, "Single entry. Sponsor details may be needed."),
    },
    ("SG", "Singapore", "🇸🇬"): {
        "tourist": ("e-visa", 3200, 3, 30, E_BASIC, "Apply at least 3 working days before travel."),
        "business": ("e-visa", 3500, 5, 30, BIZ_E, "Host company details are required."),
    },
    ("TH", "Thailand", "🇹🇭"): {
        "tourist": ("visa-free", 0, 0, 60, [], "Indian passport holders can currently enter visa-free. Carry return ticket and hotel proof."),
        "business": ("e-visa", 4500, 7, 90, BIZ_E, "Work-related activity needs the right visa."),
    },
    ("LK", "Sri Lanka", "🇱🇰"): {
        "tourist": ("e-visa", 2800, 1, 30, E_BASIC, "Electronic Travel Authorisation (ETA)."),
        "business": ("e-visa", 3500, 2, 30, BIZ_E, "Electronic Travel Authorisation (ETA)."),
    },
    ("GB", "United Kingdom", "🇬🇧"): {
        "tourist": ("sticker", 15500, 15, 180, STICKER, "Standard Visitor visa. Biometrics appointment is required."),
        "business": ("sticker", 15500, 15, 180, BIZ_STICKER, "Standard Visitor visa for business visits."),
    },
    ("US", "United States", "🇺🇸"): {
        "tourist": ("sticker", 17000, 45, 180, STICKER, "B1/B2 visa. Embassy interview is required; wait times vary a lot."),
        "business": ("sticker", 17000, 45, 180, BIZ_STICKER, "B1/B2 visa. Embassy interview is required."),
    },
    ("SCHENGEN", "Schengen (Europe)", "🇪🇺"): {
        "tourist": ("sticker", 9500, 15, 90, STICKER, "90 days within 180. Apply to the country where you spend the most nights."),
        "business": ("sticker", 9500, 15, 90, BIZ_STICKER, "Invitation from the host company is required."),
    },
    ("JP", "Japan", "🇯🇵"): {
        "tourist": ("e-visa", 2800, 5, 90, E_BASIC, "Single entry for tourism."),
        "business": ("sticker", 3500, 7, 90, BIZ_STICKER, "Guarantor / host company letter is required."),
    },
    ("VN", "Vietnam", "🇻🇳"): {
        "tourist": ("e-visa", 2800, 4, 90, E_BASIC, "Single or multiple entry e-visa."),
        "business": ("e-visa", 3500, 5, 90, BIZ_E, "Host company letter is required."),
    },
    ("ID", "Indonesia (Bali)", "🇮🇩"): {
        "tourist": ("on-arrival", 3000, 0, 30, [], "Pay the visa-on-arrival fee (about IDR 500,000) at the airport, extendable once."),
        "business": ("e-visa", 4500, 5, 60, BIZ_E, "Business visit visa."),
    },
    ("MV", "Maldives", "🇲🇻"): {
        "tourist": ("visa-free", 0, 0, 30, [], "Free 30-day visa on arrival. Carry a confirmed hotel booking and return ticket."),
        "business": ("visa-free", 0, 0, 30, [], "Free 30-day visa on arrival. Carry an invitation letter."),
    },
    ("NP", "Nepal", "🇳🇵"): {
        "tourist": ("visa-free", 0, 0, 150, [], "No visa for Indian citizens. Carry a valid photo ID (passport or voter ID)."),
        "business": ("visa-free", 0, 0, 150, [], "No visa for Indian citizens. Carry a valid photo ID."),
    },
    ("MU", "Mauritius", "🇲🇺"): {
        "tourist": ("visa-free", 0, 0, 90, [], "Visa-free. Carry return ticket and hotel booking."),
        "business": ("visa-free", 0, 0, 90, [], "Visa-free for short business visits."),
    },
    ("MY", "Malaysia", "🇲🇾"): {
        "tourist": ("visa-free", 0, 0, 30, [], "Visa-free for Indians under the current waiver. Check the waiver end date before travel."),
        "business": ("e-visa", 3500, 5, 30, BIZ_E, "Business visit e-visa."),
    },
}


def build_rules() -> list[dict]:
    rows = []
    for (code, name, flag), purposes in RULES.items():
        for purpose, (vtype, fee, days, stay, docs, notes) in purposes.items():
            rows.append({"country_code": code, "country_name": name, "flag": flag, "purpose": purpose,
                         "visa_type": vtype, "fee_inr": fee, "processing_days": days, "max_stay_days": stay,
                         "passport_min_months": 6, "docs": docs, "notes": notes})
    return rows
