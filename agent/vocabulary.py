"""Keyterms handed to Deepgram nova-3 so place names survive transcription.

This is not polish. The whole product turns on hearing a destination correctly:
a rehearsal transcribed "Bali" as "Boli", and a wrong city means a wrong flight
search, which means a wrong widget on someone's screen mid-call. Keyterm
prompting biases the acoustic model toward these exact strings.

Kept deliberately short — a keyterm list is a bias, and biasing toward 500 city
names makes the model hear city names everywhere. Origins are the Indian metros
our users actually fly from; destinations are the routes they actually search.
Add a term when a real transcript gets it wrong, not speculatively.
"""

from __future__ import annotations

# Indian origins (spec section 6: flight search is India-outbound in v1).
ORIGINS = [
    "Bangalore", "Bengaluru", "Delhi", "Mumbai", "Hyderabad", "Chennai",
    "Kolkata", "Pune", "Ahmedabad", "Goa", "Kochi", "Jaipur",
]

# The destinations that actually come up.
DESTINATIONS = [
    "Bali", "Denpasar", "Dubai", "Abu Dhabi", "Singapore", "Bangkok", "Phuket",
    "Kuala Lumpur", "Colombo", "Maldives", "Male", "Kathmandu", "Tokyo",
    "Seoul", "Hong Kong", "Istanbul", "London", "Paris", "Amsterdam", "Zurich",
    "New York", "Tbilisi", "Baku", "Almaty", "Hanoi", "Da Nang", "Saigon",
    "Ho Chi Minh", "Manali", "Leh", "Srinagar", "Andaman", "Port Blair",
]

# Carriers, because "Indigo" and "Vistara" are routinely mangled and they show
# up whenever people talk about which flight to take.
AIRLINES = [
    "IndiGo", "Vistara", "Air India", "Akasa", "SpiceJet", "Emirates",
    "Etihad", "Qatar Airways", "Singapore Airlines", "AirAsia", "Scoot",
    "Thai Airways", "Malaysia Airlines", "Garuda",
]

# Trip-planning words that carry the constraints the agent has to extract.
PLANNING = [
    "layover", "stopover", "nonstop", "red-eye", "round trip", "one way",
    "visa on arrival", "e-visa", "long weekend",
]

# The wake name itself. Omitting this was a real bug: a live call transcribed
# "Hey copilot" as "Echo Pilot", so the fast path never matched and a direct
# question went unanswered. This is the single highest-value keyterm here — every
# other word only affects *what* the agent does, this one decides *whether* it
# reacts at all. Spelling variants are included because keyterm prompting biases
# toward exact strings, and STT splits the word inconsistently.
WAKE_TERMS = ["copilot", "co-pilot", "Copilot", "hey copilot"]

KEYTERMS: list[str] = [*WAKE_TERMS, *ORIGINS, *DESTINATIONS, *AIRLINES, *PLANNING]
