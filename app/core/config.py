from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "Flight Concierge"
    WHATSAPP_VERIFY_TOKEN: str = ""
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_APP_SECRET: str = ""
    OPENAI_API_KEY: str = ""  # optional: LLM routing via OpenAI (used first if both keys are set)
    TAVILY_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_TRANSCRIBE_MODEL: str = "whisper-1"  # reads the speech in a shared reel
    ANTHROPIC_API_KEY: str = ""  # optional: LLM routing via Claude
    ROUTER_MODEL: str = "claude-haiku-4-5-20251001"
    # rzp_test_... while testing, rzp_live_... for real money. Common alternative names are accepted too.
    RAZORPAY_KEY_ID: str = Field("", validation_alias=AliasChoices("RAZORPAY_KEY_ID", "NEXT_PUBLIC_RAZORPAY_KEY_ID", "RAZORPAY_KEY", "RAZORPAY_API_KEY"))
    RAZORPAY_KEY_SECRET: str = Field("", validation_alias=AliasChoices("RAZORPAY_KEY_SECRET", "RAZORPAY_SECRET", "RAZORPAY_API_SECRET"))
    RAZORPAY_WEBHOOK_SECRET: str = ""  # set in Razorpay dashboard -> Webhooks; used to verify callbacks
    ADMIN_TOKEN: str = ""  # protects /admin/* (visa team status updates); empty = admin API disabled
    VISA_DEMO_AUTOPROGRESS: bool = False  # demo: move submitted visas to in_review and approved on a timer
    # Hotelbeds APItude (live hotel availability). Empty keys = the bot uses only the hotels in our own database.
    HOTELBEDS_API_KEY: str = ""
    HOTELBEDS_SECRET: str = ""
    HOTELBEDS_BASE_URL: str = "https://api.test.hotelbeds.com"  # test; production is https://api.hotelbeds.com
    HOTELBEDS_MARKUP_PCT: float = 0.0  # our margin on top of the net rate Hotelbeds quotes
    HOTELBEDS_RADIUS_KM: int = 25  # how far from the city centre to look for hotels
    # Hotelbeds Transfers API
    HOTELBEDS_TRANSFERS_API_KEY: str = ""
    HOTELBEDS_TRANSFERS_SECRET: str = ""
    # Duffel (live flight search and tickets). duffel_test_ tokens use the sandbox, duffel_live_ issues real tickets.
    DUFFEL_API_TOKEN: str = ""
    DUFFEL_BASE_URL: str = "https://api.duffel.com"
    DUFFEL_MARKUP_PCT: float = 0.0  # our margin on top of the fare Duffel quotes
    DUFFEL_CONTACT_EMAIL: str = ""  # lead-passenger email when the traveller gave none (needed only without online payment)
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
