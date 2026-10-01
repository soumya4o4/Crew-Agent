"""Wiring: build the Concierge with every agent. Adding a service = add its agent to this list."""
import logging

from app.agents.buddy import BuddyAgent, BuddyRepo, OpenAIBuddy
from app.agents.cab import CabAgent, CabRepo
from app.agents.concierge import Concierge
from app.agents.concierge.classifiers import LLMClassifier, OpenAIClassifier
from app.agents.concierge.router import IntentRouter
from app.agents.events import EventsAgent, EventsRepo
from app.agents.flight import FlightAgent, FlightRepo
from app.agents.forex import ForexAgent
from app.agents.hotel import HotelAgent, HotelRepo
from app.agents.nearby import NearbyAgent
from app.agents.planner import OpenAIPlanner, PlannerAgent, TripPlannerAgent
from app.agents.visa import VisaAgent, VisaRepo
from app.agents.visa.verifier import BasicVerifier, OpenAIVerifier
from app.core import places
from app.core.config import settings
from app.services.geo_service import GeoService
from app.services.razorpay_service import RazorpayGateway
from app.services.travel_advisor import TravelAdvisor

logger = logging.getLogger(__name__)


def build_concierge(supabase_client) -> Concierge:
    repo = FlightRepo(supabase_client)  # also serves the shared CoreRepo methods
    visa_repo = VisaRepo(supabase_client)
    places.load_airports(repo.list_airports())  # every city the bot understands comes from the database
    try:
        places.load_visa_countries(visa_repo.list_destinations())
    except Exception:
        logger.warning("Could not read visa countries (is the visa_rules table created?)")
    advisor = TravelAdvisor.create(settings.OPENAI_API_KEY, settings.OPENAI_MODEL) if settings.OPENAI_API_KEY else None
    gateway = RazorpayGateway()
    geo = GeoService()
    verifier = OpenAIVerifier.create(settings.OPENAI_API_KEY, settings.OPENAI_MODEL) if settings.OPENAI_API_KEY else BasicVerifier()

    agents = [
        FlightAgent(repo, gateway, advisor),
        HotelAgent(HotelRepo(supabase_client), gateway, advisor),
        CabAgent(CabRepo(supabase_client)),
        NearbyAgent(geo),
        (PlannerAgent(repo, OpenAIPlanner.create(settings.OPENAI_API_KEY, settings.OPENAI_MODEL, settings.OPENAI_TRANSCRIBE_MODEL))
         if settings.OPENAI_API_KEY else TripPlannerAgent(repo)),
        EventsAgent(EventsRepo(supabase_client)),
        VisaAgent(visa_repo, gateway, verifier),
        ForexAgent(repo),
    ]

    if settings.OPENAI_API_KEY:  # Buddy needs an LLM: without a key it simply isn't offered
        agents.append(BuddyAgent(BuddyRepo(supabase_client), OpenAIBuddy.create(settings.OPENAI_API_KEY, settings.OPENAI_MODEL), geo))

    llm = None  # no key -> keyword routing
    if settings.OPENAI_API_KEY:
        llm = OpenAIClassifier.create(settings.OPENAI_API_KEY, settings.OPENAI_MODEL)
    elif settings.ANTHROPIC_API_KEY:
        llm = LLMClassifier.create(settings.ANTHROPIC_API_KEY, settings.ROUTER_MODEL)
    return Concierge(repo, agents, IntentRouter(llm))
