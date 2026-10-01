"""Document checks: is it the right document, readable, and (for passports) what does it say?

OpenAIVerifier looks at images with a vision model. Without an API key, or for PDFs, BasicVerifier only checks the
file looks sane and flags it for our team to review by hand.
"""
import base64
import json
import re
from dataclasses import dataclass, field
from datetime import date

from app.agents.visa.rules import DOC_LABELS

MIN_IMAGE_BYTES = 15_000  # a real photo of a document is bigger than this; tiny files are usually screenshots of nothing


@dataclass
class VerifyResult:
    ok: bool
    issues: list[str] = field(default_factory=list)
    fields: dict = field(default_factory=dict)  # passport: name, passport_no, dob, expiry (ISO dates)
    note: str = ""                               # shown to the user, e.g. "our team will double-check"


def _iso(value) -> str | None:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


class BasicVerifier:
    async def verify(self, doc_type: str, data: bytes, mime: str) -> VerifyResult:
        if len(data) < MIN_IMAGE_BYTES:
            return VerifyResult(False, ["The file is very small or blurry. Please send a clear, full-size photo or PDF."])
        return VerifyResult(True, note="Received. Our team will double-check this one by hand.")


class OpenAIVerifier:
    CHECKS = {
        "passport": "the photo page of an Indian passport. Extract the holder's full name, passport number, date of birth "
                    "and date of expiry. Flag it if any of these are cut off, blurry, or covered by glare.",
        "photo": "a passport-size photo of one person: face clearly visible, plain light background, no cap or sunglasses.",
        "bank_statement": "a bank statement showing the account holder's name, the bank, and a balance or transactions "
                          "from the last few months.",
        "itinerary": "flight tickets or an itinerary showing the traveller's name and travel dates.",
        "hotel_booking": "a hotel booking confirmation showing the guest name, hotel and stay dates.",
        "invitation": "a business invitation letter on company letterhead giving dates and purpose of the visit.",
    }

    def __init__(self, client, model: str):
        self.client, self.model = client, model
        self.basic = BasicVerifier()

    @classmethod
    def create(cls, api_key: str, model: str) -> "OpenAIVerifier":
        from openai import AsyncOpenAI
        return cls(AsyncOpenAI(api_key=api_key), model)

    async def verify(self, doc_type: str, data: bytes, mime: str) -> VerifyResult:
        if not mime.startswith("image/"):  # PDFs etc: can't be read by the vision model here
            return await self.basic.verify(doc_type, data, mime)
        prompt = (f"You check documents for a visa application. The user was asked for: {DOC_LABELS.get(doc_type, doc_type)}.\n"
                  f"It should be {self.CHECKS.get(doc_type, 'the requested document')}\n"
                  'Reply with JSON only: {"is_expected_document": bool, "legible": bool, "issues": [short plain-English strings], '
                  '"name": str|null, "passport_no": str|null, "dob": "YYYY-MM-DD"|null, "expiry": "YYYY-MM-DD"|null}. '
                  "Only list real problems in issues. Never guess values you cannot read: use null.")
        url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        resp = await self.client.chat.completions.create(
            model=self.model, max_tokens=400, response_format={"type": "json_object"},
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt},
                                                   {"type": "image_url", "image_url": {"url": url}}]}])
        out = json.loads(resp.choices[0].message.content)
        issues = [str(i)[:140] for i in out.get("issues") or []][:3]
        if not out.get("is_expected_document", False):
            issues.insert(0, f"This doesn't look like a {DOC_LABELS.get(doc_type, doc_type).lower()}.")
        if not out.get("legible", True):
            issues.append("Some of it is hard to read. Please retake it in better light.")
        fields = {}
        if doc_type == "passport":
            no = re.sub(r"[^A-Z0-9]", "", str(out.get("passport_no") or "").upper())
            fields = {"name": (out.get("name") or "").strip().title() or None, "passport_no": no or None,
                      "dob": _iso(out.get("dob")), "expiry": _iso(out.get("expiry"))}
            if not fields["expiry"] or not fields["passport_no"]:
                issues.append("I couldn't read the passport number and expiry date clearly.")
        return VerifyResult(not issues, list(dict.fromkeys(issues)), fields)
