"""Which language the traveller writes in, so the bot answers in kind: plain English unless they write Hinglish.
If they ask for a language ("talk to me in Hindi"), that wins until they ask for another."""
import re

# Words that are Hindi/Hinglish and not English (so one of them is a strong signal).
HINGLISH = {"hai", "hain", "kya", "kaise", "kahan", "kaha", "mujhe", "mera", "meri", "mere", "nahi", "nahin", "kal", "aaj", "bhai",
            "yaar", "karo", "karna", "chahiye", "chiye", "chahie", "batao", "bata", "dikhao", "dikha", "jaana", "jana", "jaa", "ghoomna",
            "kitna", "kitne", "accha", "acha", "thoda", "bahut", "bohot", "aur", "mein", "raha", "rahi", "rha", "rhi", "tha", "thi",
            "abhi", "pehle", "phir", "wala", "wali", "samajh", "dekh", "dekho", "chalo", "haan", "kaisa", "kaun", "kyu", "kyun",
            "lagta", "lagega", "milega", "dena", "lena", "hu", "hoon", "apna", "apni", "tumhe", "aapko", "aap", "tum", "sab"}
WORD = re.compile(r"[a-z']+")
DEVANAGARI = re.compile(r"[\u0900-\u097f]")
ASKS_FOR = re.compile(r"\b(?:talk|speak|reply|respond|answer|write|chat|baat|bolo|bol|likho|jawab|answer)\b[^.?!]*?\b(?:in|mein|me|se)\s+(hindi|hinglish|english|angrezi)\b"
                      r"|\b(hindi|hinglish|english)\s+(?:mein|me|main)\s+(?:baat|bolo|bol|likho|jawab|reply|batao)\b", re.I)


def update_language(ctx: dict, text: str) -> None:
    """Remember in ctx["hinglish"] whether the latest messages are Hinglish. An explicit request ("talk in Hindi") sets a
    preference that sticks; otherwise one Hindi word switches Hinglish on, and a plain English sentence of 3+ words switches it off."""
    if m := ASKS_FOR.search(text or ""):
        want = (m.group(1) or m.group(2)).lower()
        ctx["lang_pref"] = "english" if want in ("english", "angrezi") else "hindi" if want == "hindi" else "hinglish"
        ctx["hinglish"] = ctx["lang_pref"] != "english"
        return
    if DEVANAGARI.search(text or ""):
        ctx["lang_pref"], ctx["hinglish"] = "hindi_script", True
        return
    if ctx.get("lang_pref"):
        return
    words = WORD.findall((text or "").lower())
    if not words:
        return
    if any(w in HINGLISH for w in words):
        ctx["hinglish"] = True
    elif len(words) >= 3:
        ctx["hinglish"] = False


def language_name(ctx: dict) -> str:
    pref = ctx.get("lang_pref")
    if pref == "hindi_script":
        return "Hindi in Devanagari script"
    if pref == "english" or (not pref and not ctx.get("hinglish")):
        return "English"
    return "Hinglish (Hindi and English mixed, Roman script)"
