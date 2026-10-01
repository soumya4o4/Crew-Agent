"""Data access shared by every agent: users, conversation state, chat history, interests.
Synchronous; callers run it in a thread."""


class CoreRepo:
    def __init__(self, client):
        self.db = client

    # ---- users / conversation state
    def get_or_create_user(self, phone: str, name: str) -> dict:
        for _ in range(2):  # second pass covers a concurrent insert of the same phone
            rows = self.db.table("users").select("*").eq("phone", phone).limit(1).execute().data
            if rows:
                return rows[0]
            try:
                return self.db.table("users").insert(
                    {"phone": phone, "name": name or "Traveller", "preferences": {}}
                ).execute().data[0]
            except Exception:
                continue
        raise RuntimeError("Could not create user")

    def get_conversation(self, phone: str) -> dict | None:
        rows = self.db.table("conversations").select("*").eq("phone", phone).limit(1).execute().data
        return rows[0] if rows else None

    def save_conversation(self, phone: str, step: str, context: dict) -> None:
        self.db.table("conversations").upsert(
            {"phone": phone, "current_step": step, "context": context}, on_conflict="phone"
        ).execute()

    # ---- chat memory
    def log_message(self, user_id: str, role: str, agent: str | None, content: str) -> None:
        self.db.table("messages").insert(
            {"user_id": user_id, "role": role, "agent": agent, "content": content[:2000]}
        ).execute()

    def recent_messages(self, user_id: str, limit: int = 8) -> list[dict]:
        rows = (self.db.table("messages").select("role, agent, content").eq("user_id", user_id)
                .order("created_at", desc=True).limit(limit).execute().data)
        return rows[::-1]  # oldest first

    # ---- long-term memory
    def add_interest(self, user_id: str, interest: str) -> None:
        """Remember which not-yet-live services this user wants (users.preferences.interests)."""
        row = self.db.table("users").select("preferences").eq("id", user_id).limit(1).execute().data
        prefs = (row[0]["preferences"] if row else None) or {}
        prefs["interests"] = sorted(set(prefs.get("interests", [])) | {interest})
        self.db.table("users").update({"preferences": prefs}).eq("id", user_id).execute()
