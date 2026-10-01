from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

class TextMessage(BaseModel):
    body: str

class MessageInfo(BaseModel):
    from_: str = Field(alias="from")
    id: str
    timestamp: str
    text: Optional[TextMessage] = None
    interactive: Optional[Dict[str, Any]] = None  # button_reply / list_reply taps
    image: Optional[Dict[str, Any]] = None        # {id, mime_type, caption}
    document: Optional[Dict[str, Any]] = None     # {id, mime_type, filename, caption}
    video: Optional[Dict[str, Any]] = None        # {id, mime_type, caption}
    location: Optional[Dict[str, Any]] = None     # {latitude, longitude, name, address}
    type: str

class ContactProfile(BaseModel):
    name: str

class ContactInfo(BaseModel):
    profile: Optional[ContactProfile] = None
    wa_id: str

class ValueInfo(BaseModel):
    messaging_product: str
    metadata: Dict[str, Any]
    contacts: Optional[List[ContactInfo]] = None
    messages: Optional[List[MessageInfo]] = None

class ChangeInfo(BaseModel):
    value: ValueInfo
    field: str

class EntryInfo(BaseModel):
    id: str
    changes: List[ChangeInfo]

class WhatsAppWebhookPayload(BaseModel):
    object: str
    entry: List[EntryInfo]
