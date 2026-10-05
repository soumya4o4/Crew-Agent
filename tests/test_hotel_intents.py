import pytest
from fakes import Chat

def test_i_want_to_book_a_hotel():
    c = Chat()
    # 1. "I want to book a hotel"
    out = c.send("I want to book a hotel")
    assert "Which city or destination" in out["body"]

def test_book_hotel():
    c = Chat()
    # 2. "book hotel"
    out = c.send("book hotel")
    assert "Which city or destination" in out["body"]

def test_i_need_a_hotel_in_dubai():
    c = Chat()
    # 3. "I need a hotel in Dubai"
    out = c.send("I need a hotel in Dubai")
    # Should skip destination and ask for date
    assert "When do you check in" in out["body"] or "Which city" not in out["body"]

def test_find_hotel():
    c = Chat()
    # 4. "find hotel"
    out = c.send("find hotel")
    assert "Which city or destination" in out["body"]

def test_hotel_intent_after_flight():
    c = Chat()
    # 5. hotel intent after flight
    c.send("flight to goa tomorrow")
    out = c.send("I need a hotel there")
    # Hotel agent should use the flight destination or ask for check in
    assert "Oops" not in out["body"]
