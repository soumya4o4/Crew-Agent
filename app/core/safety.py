"""Safety net for messages about self-harm or immediate danger.

Detection here is plain pattern matching, so it works even when the LLM is down or not configured. The Concierge
checks every typed message with is_crisis() before anything else. The Buddy's LLM can also flag a risk the patterns
missed; it then appends the matching note below to its reply.

Helplines (India): re-check these numbers before launch, they can change.
"""
import re

_SELF_HARM = re.compile(
    r"\b(suicide|suicidal|kill myself|killing myself|end my life|end it all|want to die|wanna die|take my (?:own )?life|"
    r"self[- ]?harm|hurt myself|cut myself|khudkushi|khudkhushi|"
    r"mar (?:jaana|jana|jau|jaun) (?:chahta|chahti)|marna (?:chahta|chahti)|"
    r"zindagi (?:khatam|se thak)|jeene ka (?:mann|man|mood) nahi|"
    r"jeena nahi (?:chahta|chahti)|jeene ki (?:iccha|ichha) nahi)\b")

HELPLINES = ("📞 *Tele-MANAS*: 14416 (free, 24x7, many languages)\n"
             "📞 *iCall*: 022-25521111 (Mon-Sat, 8 AM-10 PM)")

CRISIS_MESSAGE = (
    "Tumne ye mujhse share kiya, achha kiya, aur tumhari baat mujhe sach mein important lagti hai. 💛\n\n"
    "Tum akele nahi ho. Abhi kisi insaan se baat karna sabse zyada madad karega:\n"
    f"{HELPLINES}\n"
    "🚨 Agar abhi khud ko nuksan pahunchane ka khatra hai, *112* par call karo ya kisi apne ko paas bula lo.\n\n"
    "Main yahin hoon, tumhari baat sunne ke liye. Jo chal raha hai, bolo.")

# Appended to a Buddy reply when the LLM spots a risk the patterns missed.
RISK_NOTES = {
    "self_harm": f"\n\n💛 Agar mann mein khud ko nuksan pahunchane ke khayal aa rahe hain, kisi se zaroor baat karo:\n{HELPLINES}\n"
                 "Khatra abhi hai to *112*.",
    "danger": "\n\n🚨 Agar tum ya koi aur abhi khatre mein hai, turant *112* par call karo. Safe jagah par jao aur kisi bharose ke insaan ko batao.",
}


def is_crisis(text: str) -> bool:
    return bool(_SELF_HARM.search(text.lower()))
