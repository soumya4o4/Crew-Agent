from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "Flight Concierge"
    WHATSAPP_VERIFY_TOKEN: str = ""
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_APP_SECRET: str = ""
    OPENAI_API_KEY: str = ""  # optional: LLM routing via OpenAI (used first if both keys are set)
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
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
