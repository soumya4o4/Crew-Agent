"""Contracts shared by every agent."""
from dataclasses import dataclass, field


@dataclass
class Session:
    """One user's state for the current message. Persisted per phone in `conversations`.

    ctx is shared by all agents; `ctx["trip"]` carries trip facts (destination, date) between them,
    and `ctx["agent"]` records which agent currently owns the conversation.
    """
    phone: str
    user: dict
    step: str
    msg_id: str = ""
    ctx: dict = field(default_factory=dict)


class Agent:
    """A specialist (flights, hotels, ...) that the Concierge routes to.

    owns:   the button/list id prefixes ("kind" in "kind:value") this agent handles.
    process() takes the user's text or tapped id and returns the messages to send back.
    """
    name: str = ""
    title: str = ""
    emoji: str = ""
    menu_desc: str = ""
    in_menu: bool = True  # False: not a row in the main menu (still reachable by text, buttons and shared files)
    owns: frozenset[str] = frozenset()

    def reset(self, s: Session) -> None:
        """Forget any half-finished work (called when the user returns to the main menu)."""

    def expects_text(self, s: Session) -> bool:
        """True while the agent is waiting for a typed answer (a date, a name...)."""
        return False

    def expects_media(self, s: Session) -> bool:
        """True while the agent is waiting for a photo or PDF from the user."""
        return False

    async def on_media(self, s: Session, media: dict, text: str) -> list[dict]:
        """media = {"id", "mime", "filename", "kind"}; text is the caption, if any."""
        raise NotImplementedError

    def expects_location(self, s: Session) -> bool:
        """True while the agent is waiting for the user to share a location pin."""
        return False

    async def on_location(self, s: Session, loc: dict) -> list[dict]:
        """loc = {"lat", "lon", "name", "address", "at"}; it is also saved in ctx["loc"]."""
        raise NotImplementedError

    def claims(self, text: str) -> bool:
        """True if this agent wants this typed message wherever the user is in the bot (a reel link, "forget me")."""
        return False

    def accepts_media(self, media: dict) -> bool:
        """True if this agent takes this file even though it did not ask for one (a reel video)."""
        return False

    def keeps_text(self, text: str, other, known: set[str]) -> bool:
        """While expects_text() is true: should this typed message still go to this agent, given what the keyword
        classifier made of it (`other`, an Intent) and the names of all agents (`known`)? False hands it to the
        Concierge, which then acts on `other` (a topic switch). Default: stay, unless another service was named."""
        return other.name not in known or self.name in (other.name, *other.also)

    async def on_enter(self, s: Session) -> list[dict]:
        raise NotImplementedError

    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        raise NotImplementedError

    async def start(self, s: Session, slots: dict) -> list[dict]:
        """Enter the agent with details the user already gave in free text (cities, date)."""
        return await self.on_enter(s)
