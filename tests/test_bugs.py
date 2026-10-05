import pytest
from datetime import date
from tests.fakes import Chat, FakeGateway
from app.agents.hotel.nlu import parse_stay
from app.core.utils import now_ist

def test_hotel_bookings_intent_override():
    c = Chat()
    c.send('book hotel in dubai')
    # User asks for bookings in the middle of hotel search
    out = c.send('show my bookings')
    assert 'flights' in out['body'].lower() or 'no bookings' in out['body'].lower()
    
def test_hotel_date_checkout_preserve():
    c = Chat()
    c.send('book hotel in dubai')
    c.send('12 october to 20 october')
    # Check that state correctly preserved check_out
    h = c.ctx.get('hotel')
    assert h['check_in'].endswith('-10-12')
    assert h['check_out'].endswith('-10-20')
    assert h['nights'] == 8

def test_hotel_idempotency():
    c = Chat(gateway=FakeGateway())
    c.send('book hotel in dubai')
    c.send('12 october to 20 october')
    out1 = c.send('2 guests')
    
    # We call agent._show_results directly with same state and message ID
    import asyncio
    out2 = asyncio.run(c.concierge.agents['hotel']._show_results(c._session()))
    assert out2 == []  # Idempotency prevents doing it again
