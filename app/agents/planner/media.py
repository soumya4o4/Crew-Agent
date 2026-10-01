"""Turning a shared reel into something a model can read: a few frames, the spoken audio, and the text of a link.

A video the user sends goes through ffmpeg (a system binary, or the one bundled in the `imageio-ffmpeg` package) with a timeout
and size limits. Links are only ever fetched for a short allow-list of video sites, over https, to read the public title and
caption (YouTube and TikTok oEmbed, Instagram's public page tags); we never download the video from a link. When a site
refuses, the planner asks the user for the video or a screenshot instead.
"""
import asyncio
import html
import io
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

logger = logging.getLogger(__name__)
MAX_VIDEO_BYTES = 16_000_000  # WhatsApp's own limit for videos
MAX_IMAGE_BYTES = 8_000_000
FRAMES = 6
FRAME_PX = 640
FFMPEG_TIMEOUT_S = 40
ALLOWED_HOSTS = ("instagram.com", "youtube.com", "youtu.be", "tiktok.com")
_URL = re.compile(r"https?://[^\s<>\"']+")
_META = re.compile(r'<meta\s+(?:property|name)=["\']og:(title|description)["\']\s+content=["\']([^"\']*)["\']', re.I)


# ---------------------------------------------------------------------------- links
def host_allowed(url: str) -> bool:
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def find_reel_url(text: str) -> str | None:
    """The first link in a message that points at a supported video site (http links are upgraded to https)."""
    for raw in _URL.findall(text or ""):
        url = raw.rstrip(".,)!?")
        url = "https://" + url[len("http://"):] if url.startswith("http://") else url
        if host_allowed(url):
            return url
    return None


class LinkReader:
    """Reads the public title and caption of a reel link: {"title", "description"} (either may be empty)."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport  # tests pass httpx.MockTransport

    async def read(self, url: str) -> dict:
        if not host_allowed(url):
            return {"title": "", "description": ""}
        host = (urlparse(url).hostname or "").lower()
        try:
            async with httpx.AsyncClient(timeout=8, transport=self._transport, headers={"User-Agent": "CrewBuddy/0.1"}) as http:
                if "youtu" in host:
                    return await self._oembed(http, f"https://www.youtube.com/oembed?format=json&url={quote(url, safe='')}")
                if "tiktok" in host:
                    return await self._oembed(http, f"https://www.tiktok.com/oembed?url={quote(url, safe='')}")
                resp = await http.get(url, follow_redirects=False)  # a redirect (usually to a login page) means "no public caption"
                tags = dict(_META.findall(resp.text[:300_000])) if resp.status_code == 200 else {}
                return {"title": html.unescape(tags.get("title", "")), "description": html.unescape(tags.get("description", ""))}
        except Exception as exc:
            logger.warning("Could not read the reel link: %s", type(exc).__name__)
            return {"title": "", "description": ""}

    @staticmethod
    async def _oembed(http: httpx.AsyncClient, endpoint: str) -> dict:
        resp = await http.get(endpoint)
        resp.raise_for_status()
        data = resp.json()
        return {"title": data.get("title", ""), "description": data.get("author_name", "")}


# ---------------------------------------------------------------------------- video
def ffmpeg_path() -> str | None:
    if found := shutil.which("ffmpeg"):
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _duration_s(ffmpeg: str, path: Path) -> float:
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0


def extract_from_video(data: bytes, frames: int = FRAMES) -> tuple[list[bytes], bytes | None]:
    """A few evenly spaced JPEG frames and the audio as a small mono MP3 (None when there is none). Blocking: run it in a thread.
    Without ffmpeg both come back empty and the planner works from the caption alone."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg or not data:
        return [], None
    with tempfile.TemporaryDirectory(prefix="reel_") as tmp:
        src, folder = Path(tmp) / "in.mp4", Path(tmp)
        src.write_bytes(data)
        try:
            duration = _duration_s(ffmpeg, src)
            rate = f"{frames / duration:.4f}" if duration > 0 else "1"
            subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(src), "-vf", f"fps={rate},scale={FRAME_PX}:-2", "-frames:v", str(frames),
                            "-q:v", "5", str(folder / "f_%02d.jpg")], capture_output=True, timeout=FFMPEG_TIMEOUT_S)
            subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", "-t", "120",
                            str(folder / "a.mp3")], capture_output=True, timeout=FFMPEG_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("ffmpeg could not read the video")
        images = [p.read_bytes() for p in sorted(folder.glob("f_*.jpg"))]
        audio = (folder / "a.mp3").read_bytes() if (folder / "a.mp3").exists() else None
    return images, audio if audio and len(audio) > 2000 else None


async def video_to_parts(data: bytes) -> tuple[list[bytes], bytes | None]:
    return await asyncio.to_thread(extract_from_video, data)


# ---------------------------------------------------------------------------- images
def shrink_image(data: bytes, max_px: int = 1024) -> bytes:
    """A screenshot can be huge; the model needs far less. Falls back to the original if Pillow can't read it."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.thumbnail((max_px, max_px))
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=80)
        return out.getvalue()
    except Exception:
        return data
