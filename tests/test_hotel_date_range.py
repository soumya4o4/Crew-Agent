import pytest
from tests.fakes import Chat

def test_hotel_date_range_parsing():
    c = Chat()
    c.send("book hotel in dubai")
    out = c.send("12 october to 20 october")
    assert "Check-out" in out["body"] or "8 nights" in out["body"] or "guests" in out["body"].lower()

def test_hotel_date_range_parsing_2():
    c = Chat()
    c.send("book hotel in dubai")
    out = c.send("12 october to 20")
    assert "Check-out" in out["body"] or "8 nights" in out["body"] or "guests" in out["body"].lower()

def test_hotel_date_range_parsing_3():
    c = Chat()
    c.send("book hotel in dubai")
    out = c.send("from 12 October to 20 October")
    assert "Check-out" in out["body"] or "8 nights" in out["body"] or "guests" in out["body"].lower()

def test_hotel_date_range_parsing_4():
    c = Chat()
    c.send("book hotel in dubai")
    out = c.send("check in 12 October and check out 20 October")
    assert "Check-out" in out["body"] or "8 nights" in out["body"] or "guests" in out["body"].lower()

