"""Planner fakes: a scripted brain, and stand-ins for the WhatsApp download, ffmpeg, link reading and sending."""
from app.agents.planner.analyzer import Insight


class FakePlannerBrain:
    """Script: .insight (what it 'sees'), .none (can't tell), .fail, .transcript. Every call is recorded."""

    def __init__(self):
        self.insight = Insight(label="Palolem Beach, South Goa, India", city_code="GOI", places=["Palolem Beach", "Cabo de Rama Fort"],
                               activities=["kayaking", "beach shacks"], vibe="Laid-back beaches", season="Nov to Feb", days=3, confidence="high")
        self.none, self.fail, self.transcript, self.calls, self.plans = False, False, "spoken words", [], []

    async def analyze(self, frames, transcript="", text=""):
        self.calls.append({"frames": frames, "transcript": transcript, "text": text})
        if self.fail:
            raise RuntimeError("openai down")
        return None if self.none else self.insight

    async def transcribe(self, audio):
        return self.transcript

    async def itinerary(self, insight, days):
        self.plans.append((insight.label, days))
        return {"days": [{"title": f"Theme {i + 1}", "morning": "Subah ghoomo", "afternoon": "Lunch aur rest", "evening": "Sunset"} for i in range(days)],
                "tips": ["Sunscreen le jao"]}


class FakeMedia:
    """download / video_parts / links / send for PlannerAgent(background=False)."""

    def __init__(self):
        self.downloads, self.sent, self.too_big, self.audio = [], [], False, b"audio"
        self.link_meta = {"title": "Goa shack life", "description": "@traveller"}

    async def download(self, media_id, max_bytes):
        self.downloads.append((media_id, max_bytes))
        if self.too_big:
            raise ValueError("file too large")
        return b"media-bytes", "video/mp4"

    async def video_parts(self, data):
        return [b"f1", b"f2"], self.audio

    async def read(self, url):  # the LinkReader interface
        return dict(self.link_meta)

    async def send(self, number, msg):
        self.sent.append((number, msg))
