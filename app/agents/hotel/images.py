"""Hotel photos. A hotel's own `image_url` wins; hotels without one get a stock photo that matches their star rating,
chosen by name so a hotel always shows the same picture. All of these are public https links WhatsApp can fetch."""
import zlib

_BASE = "https://images.unsplash.com/photo-{}?auto=format&fit=crop&w=800&q=70"
_POOL = {  # star rating -> Unsplash photo ids (checked: each returns a JPEG)
    2: ["1496417263034-38ec4f0b665a", "1517840901100-8179e982acb7", "1631049307264-da0ec9d70304", "1455587734955-081b22074882"],
    3: ["1618773928121-c32242e63f39", "1611892440504-42a792e24d32", "1590490360182-c33d57733427", "1455587734955-081b22074882"],
    4: ["1551882547-ff40c63fe5fa", "1542314831-068cd1dbfeeb", "1571896349842-33c89424de2d", "1578683010236-d716f9a3f461",
        "1568084680786-a84f91d1153c"],
    5: ["1566073771259-6a8506099945", "1564501049412-61c2a3083791", "1520250497591-112f2f40a3f4", "1561501900-3701fa6a0864",
        "1584132967334-10e028bd69f7", "1445019980597-93fa8acb246c", "1582719508461-905c673771fd"],
}


def hotel_image(h: dict) -> str:
    if h.get("image_url"):
        return h["image_url"]
    pool = _POOL.get(h.get("stars"), _POOL[3])
    return _BASE.format(pool[zlib.crc32(h.get("name", "").encode()) % len(pool)])
