import asyncio
import json
from datetime import date
import shutil
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from fakes import Chat, FakeRepo, WA
from fakes_planner import FakeMedia, FakePlannerBrain
from app.agents.base import Session
from app.agents.planner import PlannerAgent, media as M
from app.agents.planner.analyzer import (Insight, OpenAIPlanner, city_code_for, format_itinerary, parse_insight, parse_itinerary)
from app.services.whatsapp_service import WhatsAppService

REEL = "https://www.instagram.com/reel/Cxyz123/?igsh=abc"


def planner_chat():
    return Chat(planner=True)


def card_buttons(c):
    return [i for i, _ in c.last[-1]["buttons"]]


# ------------------------------------------------------------------------ the flow
def test_a_reel_link_with_a_caption_becomes_a_trip_card():
    c = planner_chat()
    c.send(f"{REEL} goa vibes")
    assert [m["type"] for m in c.last] == ["text", "buttons"] and "Dekh rahe hain" in c.last[0]["body"]
    card = c.last[1]["body"]
    assert "*Palolem Beach, South Goa, India*" in card and "📍 Palolem Beach · Cabo de Rama Fort" in card and "Nov to Feb" in card
    assert "book kar sakte hain" in card and card_buttons(c) == ["planner:plan", "planner:fix", "nav:menu"]
    call = c.planner_brain.calls[0]
    assert call["frames"] == [] and "goa vibes" in call["text"] and "Goa shack life" in call["text"] and REEL not in call["text"]
    assert c.repo.convos["+" + WA]["current_step"] == "planner_review"


def test_a_link_with_no_caption_asks_for_a_video_or_screenshot():
    c = planner_chat()
    c.media.link_meta = {"title": "", "description": ""}
    c.send(REEL)
    assert "video* ya *screenshot*" in c.last[-1]["body"] and c.planner_brain.calls == []
    assert c.repo.convos["+" + WA]["current_step"] == "planner_fix"


def test_a_forwarded_video_is_read_for_frames_and_speech():
    c = planner_chat()
    c.send_file("goa trip!", mime="video/mp4", kind="video")
    assert [m["type"] for m in c.last] == ["text", "buttons"]
    assert c.media.downloads == [("media1", M.MAX_VIDEO_BYTES)]
    call = c.planner_brain.calls[0]
    assert call["frames"] == [b"f1", b"f2"] and call["transcript"] == "spoken words" and call["text"] == "goa trip!"


def test_a_video_without_sound_still_works():
    c = planner_chat()
    c.media.audio = None
    c.send_file(mime="video/mp4", kind="video")
    assert c.planner_brain.calls[0]["transcript"] == "" and c.planner_brain.calls[0]["frames"]


def test_a_screenshot_is_read_when_nothing_else_asked_for_a_file():
    c = planner_chat()
    c.send_file(mime="image/jpeg", kind="image")
    assert c.media.downloads == [("media1", M.MAX_IMAGE_BYTES)] and len(c.planner_brain.calls[0]["frames"]) == 1
    assert c.last[-1]["type"] == "buttons"


def test_when_the_reel_cannot_be_read_the_user_is_asked_to_help():
    c = planner_chat()
    c.media.too_big = True
    c.send_file(mime="video/mp4", kind="video")
    assert "bahut bada" in c.last[-1]["body"] and c.repo.convos["+" + WA]["current_step"] == "planner_fix"
    c.media.too_big, c.planner_brain.none = False, True
    c.send_file(mime="video/mp4", kind="video")
    assert "pakki samajh nahi aayi" in c.last[-1]["body"]
    c.planner_brain.none, c.planner_brain.fail = False, True
    c.send(REEL)
    assert "dikkat aayi" in c.last[-1]["body"]


def test_a_low_confidence_guess_says_so():
    c = planner_chat()
    c.planner_brain.insight = Insight(label="Somewhere, Goa", city_code="GOI", confidence="low")
    c.send(REEL)
    assert "Pakka nahi hai" in c.last[-1]["body"]


def test_typing_a_place_works_and_so_does_correcting_a_wrong_guess():
    c = planner_chat()
    c.send("hi"); c.send(reply_id="svc:planner")
    assert "link" in c.last[0]["body"] and c.repo.convos["+" + WA]["current_step"] == "planner_wait"
    c.send("goa 3 din")
    assert c.last[0]["type"] == "buttons" and c.planner_brain.calls[0]["text"] == "goa 3 din"  # no "reading your reel" wait for a few words
    c.send(reply_id="planner:fix")
    c.send("Munnar")
    assert c.planner_brain.calls[1]["text"] == "Munnar"


def test_free_text_with_a_known_city_skips_reading_anything():
    c = planner_chat()
    c.send("plan a trip to goa")
    assert c.planner_brain.calls == [] and "*Goa*" in c.last[-1]["body"] and card_buttons(c)[0] == "planner:plan"


# ------------------------------------------------------------------- the plan and booking
def make_plan(c, days=3):
    c.send(REEL)
    days_list = c.send(reply_id="planner:plan")
    c.send(reply_id=f"rdays:{days}")
    return days_list


def test_the_user_picks_the_days_and_gets_a_day_by_day_plan():
    c = planner_chat()
    rows = make_plan(c)["rows"]
    assert [r[0] for r in rows] == [f"rdays:{n}" for n in range(1, 8)] and rows[2][2] == "Suggested for this reel"
    plan = c.last[0]["body"]
    assert "3 din, Palolem Beach" in plan and "*Day 1 · Theme 1*" in plan and "*Day 3 · Theme 3*" in plan and "Sunscreen le jao" in plan
    assert c.planner_brain.plans == [("Palolem Beach, South Goa, India", 3)]
    assert card_buttons(c) == ["planner:book", "planner:days", "nav:menu"]


def test_booking_the_trip_hands_over_to_flights_and_keeps_the_rest_queued():
    c = planner_chat()
    make_plan(c)
    c.send(reply_id="planner:book")
    assert "Chalo, pehle flights" in c.last[0]["body"] and "from:IDR" in c.ids()  # the flight flow starts with Goa as the destination
    ctx = c.repo.convos["+" + WA]["context"]
    assert ctx["queue"] == ["hotel", "cab", "events"] and ctx["pre_to"] == "GOI"


def test_the_hotel_flow_knows_the_trip_length():
    c = planner_chat()
    make_plan(c, days=4)
    c.send(reply_id="planner:book")
    c.repo.convos["+" + WA]["context"]["trip"] = {"from": "IDR", "to": "GOI", "city": "Goa", "date": date.today().isoformat(), "arrival": None}
    c.send(reply_id="svc:hotel")
    c.send(reply_id="hotel:trip")
    assert c.ids() == ["hgst:1", "hgst:2", "hgst:3", "hgst:4"]  # city, check-in and 3 nights (4 days) are already known


def test_a_place_we_cannot_book_still_gets_a_plan_and_a_map():
    c = planner_chat()
    c.planner_brain.insight = Insight(label="Munnar, Kerala, India", city_code=None, places=["Tea gardens"], confidence="high")
    c.send(REEL)
    assert "booking abhi hamare paas nahi" in c.last[-1]["body"]
    c.send(reply_id="planner:plan"); c.send(reply_id="rdays:2")
    assert [m["type"] for m in c.last] == ["text", "cta", "buttons"]
    assert "destination=Munnar%2C+Kerala%2C+India" in c.last[1]["url"] and "planner:book" not in card_buttons(c)


def test_a_failed_plan_can_be_retried():
    c = planner_chat()
    c.send(REEL); c.send(reply_id="planner:plan")

    async def boom(insight, days):
        raise RuntimeError("down")

    c.planner_brain.itinerary = boom
    out = c.send(reply_id="rdays:3")
    assert "Plan banane mein dikkat" in out["body"] and "rdays:3" in card_buttons(c)


def test_stale_buttons_do_not_crash():
    c = planner_chat()
    assert c.send(reply_id="rdays:3")["type"] == "text"  # no plan yet: starts over
    assert c.send(reply_id="planner:book")["type"] == "text"


# -------------------------------------------------------------------- background mode
def test_in_the_background_the_user_gets_an_answer_at_once_and_the_result_later():
    repo, brain, media = FakeRepo(), FakePlannerBrain(), FakeMedia()
    agent = PlannerAgent(repo, brain, media, media.download, media.send, media.video_parts, background=True)
    user = repo.get_or_create_user("+" + WA, "Aarav Sharma")
    s = Session("+" + WA, user, "menu", "m1", {})

    async def go():
        out = await agent.process(s, REEL, None)
        assert s.step == "planner_busy" and brain.calls == [] and media.sent == []  # still working
        await asyncio.gather(*agent._tasks)
        return out

    out = asyncio.run(go())
    assert [m["body"][:12] for m in out] == ["🎬 Reel mil g"]
    assert [n for n, _ in media.sent] == [WA] and "Palolem Beach" in media.sent[0][1]["body"]
    saved = repo.convos["+" + WA]
    assert saved["current_step"] == "planner_review" and saved["context"]["plan"]["city_code"] == "GOI" and saved["context"]["agent"] == "planner"


def test_a_crash_in_the_background_still_tells_the_user():
    repo, brain, media = FakeRepo(), FakePlannerBrain(), FakeMedia()
    brain.fail = True
    agent = PlannerAgent(repo, brain, media, media.download, media.send, media.video_parts, background=True)
    asyncio.run(agent._deliver("+" + WA, {"media": None, "link": REEL, "text": ""}))
    assert "dikkat aayi" in media.sent[0][1]["body"] and repo.convos["+" + WA]["current_step"] == "planner_fix"


# ------------------------------------------------------------------------- media helpers
@pytest.mark.parametrize("text,expected", [
    (f"look {REEL} wow", REEL),
    ("http://youtu.be/abc123", "https://youtu.be/abc123"),
    ("https://www.tiktok.com/@x/video/1.", "https://www.tiktok.com/@x/video/1"),
    ("https://evil.com/instagram.com", None), ("https://instagram.com.evil.com/reel/x", None),
    ("https://localhost/reel", None), ("no link here", None)])
def test_only_links_to_the_supported_sites_are_taken(text, expected):
    assert M.find_reel_url(text) == expected


def test_link_reader_reads_oembed_and_public_tags_and_stops_at_a_login_wall():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "www.youtube.com":
            return httpx.Response(200, json={"title": "Goa in 60 seconds", "author_name": "Traveller"})
        if request.url.host == "www.tiktok.com":
            return httpx.Response(200, json={"title": "Baga beach vlog", "author_name": "x"})
        if request.url.path.startswith("/reel/ok"):
            return httpx.Response(200, text='<meta property="og:title" content="Aarav on Instagram: &quot;Palolem&quot;"><meta property="og:description" content="Kayaking in Goa">')
        return httpx.Response(302, headers={"location": "https://www.instagram.com/accounts/login/"})

    reader = M.LinkReader(httpx.MockTransport(handler))
    run = lambda url: asyncio.run(reader.read(url))
    assert run("https://youtu.be/abc") == {"title": "Goa in 60 seconds", "description": "Traveller"}
    assert run("https://www.tiktok.com/@x/video/1")["title"] == "Baga beach vlog"
    assert run("https://www.instagram.com/reel/ok1/") == {"title": 'Aarav on Instagram: "Palolem"', "description": "Kayaking in Goa"}
    assert run("https://www.instagram.com/reel/walled/") == {"title": "", "description": ""}
    seen.clear()
    assert run("https://evil.com/x") == {"title": "", "description": ""} and seen == []  # never fetched


FFMPEG = M.ffmpeg_path()


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_frames_and_audio_come_out_of_a_real_video(tmp_path):
    def make(name, with_audio):
        out = tmp_path / name
        args = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10"]
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=4"] if with_audio else []
        subprocess.run(args + ["-pix_fmt", "yuv420p", str(out)], check=True, capture_output=True)
        return out.read_bytes()

    frames, audio = M.extract_from_video(make("a.mp4", True))
    assert len(frames) == M.FRAMES and all(f.startswith(b"\xff\xd8") for f in frames) and audio and len(audio) > 2000
    frames, audio = M.extract_from_video(make("b.mp4", False))
    assert len(frames) == M.FRAMES and audio is None
    assert M.extract_from_video(b"not a video") == ([], None)


def test_images_are_shrunk_and_junk_is_left_alone():
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), "red").save(buf, "PNG")
    small = M.shrink_image(buf.getvalue())
    assert Image.open(io.BytesIO(small)).size[0] <= 1024
    assert M.shrink_image(b"junk") == b"junk"


def test_incoming_video_message_is_parsed():
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {}, "contacts": [{"profile": {"name": "Aarav"}, "wa_id": WA}],
        "messages": [{"from": WA, "id": "wamid.1", "timestamp": "1", "type": "video", "video": {"id": "vid1", "mime_type": "video/mp4", "caption": "goa!"}}]}}]}]}
    msg = WhatsAppService.extract_message_data(payload)
    assert msg["media"] == {"id": "vid1", "mime": "video/mp4", "filename": "", "kind": "video"} and msg["text"] == "goa!"


# ------------------------------------------------------------------------- the brain
def test_insight_parsing_is_defensive():
    ok = parse_insight(json.dumps({"found": True, "label": "Baga <b>Beach</b>, Goa, India", "places": ["Baga", "", 5, "x" * 200],
                                   "activities": ["water sports"], "vibe": "party", "best_season": "Nov-Feb", "suggested_days": 99, "confidence": "certain"}))
    assert ok.label == "Baga (b)Beach(/b), Goa, India" and ok.city_code == "GOI" and ok.days == 3 and ok.confidence == "low"
    assert ok.places[0] == "Baga" and len(ok.places) == 3 and ok.short == "Baga (b)Beach(/b)"
    assert parse_insight(json.dumps({"found": False})) is None and parse_insight(json.dumps({"found": True, "label": ""})) is None
    assert parse_insight(json.dumps({"found": True, "label": "Munnar, Kerala, India"})).city_code is None
    with pytest.raises(ValueError):
        parse_insight("not json")
    assert city_code_for("Burj Khalifa, Dubai, UAE") == "DXB" and city_code_for("Taj Mahal, Agra") is None


def test_itinerary_parsing_and_formatting():
    raw = json.dumps({"days": [{"title": f"D{i}", "morning": "m" * 300, "afternoon": "a", "evening": ""} for i in range(9)], "tips": ["t"] * 9})
    plan = parse_itinerary(raw, 3)
    assert len(plan["days"]) == 3 and len(plan["days"][0]["morning"]) == 220 and len(plan["tips"]) == 4
    titled = parse_itinerary(json.dumps({"days": [{"title": t} for t in ("Day 1 - Heritage", "DAY 2: Beaches", "Din 3 · Fort", "Sunset walk")]}), 4)
    assert [d["title"] for d in titled["days"]] == ["Heritage", "Beaches", "Fort", "Sunset walk"]  # we number the days ourselves
    with pytest.raises(ValueError):
        parse_itinerary(json.dumps({"days": []}), 3)
    messages = format_itinerary(Insight(label="Goa, India"), parse_itinerary(json.dumps({"days": [{"title": "x", "morning": "m", "afternoon": "a", "evening": "e"}]}), 1))
    assert len(messages) == 1 and "🌅 m" in messages[0] and "🌙 e" in messages[0]
    long_plan = {"days": [{"title": "x", "morning": "m" * 220, "afternoon": "a" * 220, "evening": "e" * 220}] * 7, "tips": ["t" * 140] * 4}
    pieces = format_itinerary(Insight(label="Goa, India"), long_plan)
    assert len(pieces) > 1 and all(len(p) < 3500 for p in pieces)


def test_the_openai_calls_send_frames_text_and_ask_for_json():
    seen = []

    async def create(**kwargs):
        seen.append(kwargs)
        content = json.dumps({"found": True, "label": "Goa, India"}) if "frames" in kwargs["messages"][0]["content"] else json.dumps(
            {"days": [{"title": "t", "morning": "m", "afternoon": "a", "evening": "e"}], "tips": []})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    async def transcribe(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(text="hello goa")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                             audio=SimpleNamespace(transcriptions=SimpleNamespace(create=transcribe)))
    brain = OpenAIPlanner(client, "test-model", "test-whisper")
    insight = asyncio.run(brain.analyze([b"x"] * 9, "spoken", "caption text"))
    sent = seen[0]
    assert insight.city_code == "GOI" and sent["model"] == "test-model" and sent["response_format"] == {"type": "json_object"}
    parts = sent["messages"][1]["content"]
    assert "caption text" in parts[0]["text"] and "spoken" in parts[0]["text"]
    assert len(parts) == 1 + 6 and parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,") and parts[1]["image_url"]["detail"] == "low"
    assert asyncio.run(brain.itinerary(insight, 1))["days"][0]["title"] == "t" and "Days: 1" in seen[1]["messages"][1]["content"]
    assert asyncio.run(brain.transcribe(b"audio")) == "hello goa" and seen[2]["model"] == "test-whisper"
