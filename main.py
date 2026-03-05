"""
SnapLoad Railway Backend — main.py (Pydantic v1 Compatible)
FastAPI + yt-dlp universal video extractor.
"""

import logging
import os
import time
from typing import Optional, List
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, validator

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("snapload")

# ============================================================
# APP
# ============================================================
app = FastAPI(
    title="SnapLoad Video Extractor API",
    description="Universal video downloader — 1000+ sites via yt-dlp",
    version="1.0.0",
)

# ============================================================
# CORS
# ============================================================
allowed_origins_raw = os.getenv("ALLOWED_ORIGINS", "*")
allowed_origins = (
    ["*"]
    if allowed_origins_raw.strip() == "*"
    else [o.strip() for o in allowed_origins_raw.split(",") if o.strip()]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ============================================================
# MODELS (Pydantic v1 syntax)
# ============================================================
class ExtractRequest(BaseModel):
    url: str

    @validator("url")
    def validate_url(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("URL cannot be empty.")
        try:
            parsed = urlparse(v)
            if parsed.scheme not in ("http", "https"):
                raise ValueError("URL must use http or https.")
            if not parsed.netloc:
                raise ValueError("Invalid URL format.")
        except Exception as e:
            raise ValueError(f"Invalid URL: {e}")
        blocked = ["localhost", "127.0.0.1", "0.0.0.0"]
        for b in blocked:
            if b in parsed.netloc:
                raise ValueError("URL not allowed.")
        return v


# ============================================================
# HELPERS
# ============================================================
def is_tiktok(url: str) -> bool:
    return "tiktok.com" in url.lower()


def detect_platform(url: str) -> str:
    u = url.lower()
    platforms = {
        "youtube.com": "YouTube", "youtu.be": "YouTube",
        "tiktok.com": "TikTok", "instagram.com": "Instagram",
        "facebook.com": "Facebook", "fb.watch": "Facebook",
        "twitter.com": "Twitter/X", "x.com": "Twitter/X",
        "reddit.com": "Reddit", "vimeo.com": "Vimeo",
        "twitch.tv": "Twitch", "soundcloud.com": "SoundCloud",
        "dailymotion.com": "Dailymotion",
    }
    for domain, name in platforms.items():
        if domain in u:
            return name
    try:
        netloc = urlparse(url).netloc.replace("www.", "")
        return netloc.split(".")[0].capitalize()
    except Exception:
        return "Unknown"


def build_ydl_opts(url: str) -> dict:
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "geo_bypass": True,
        "geo_bypass_country": "US",
        "socket_timeout": 20,
        "retries": 3,
    }
    if os.getenv("COOKIES_FILE"):
        opts["cookiefile"] = os.getenv("COOKIES_FILE")
    if os.getenv("HTTP_PROXY"):
        opts["proxy"] = os.getenv("HTTP_PROXY")

    # TikTok no-watermark
    if is_tiktok(url):
        opts["extractor_args"] = {
            "tiktok": {
                "app_name": ["trill"],
                "app_version": ["26.1.3"],
                "api_hostname": ["api22-normal-c-alisg.tiktokv.com"],
            }
        }
        logger.info("TikTok — no-watermark mode enabled")

    return opts


def format_duration(seconds) -> Optional[str]:
    if not seconds:
        return None
    try:
        total = int(float(seconds))
        m, s = divmod(total, 60)
        return f"{m}:{s:02d}"
    except Exception:
        return str(seconds)


def process_formats(info: dict, url: str) -> list:
    raw = info.get("formats", [])
    if not raw:
        raw = [{
            "format_id": "default",
            "ext": info.get("ext", "mp4"),
            "url": info.get("url", ""),
            "vcodec": info.get("vcodec", "unknown"),
            "acodec": info.get("acodec", "unknown"),
            "tbr": info.get("tbr"),
            "filesize": info.get("filesize"),
        }]

    processed = []
    seen = set()
    _tiktok = is_tiktok(url)

    for fmt in raw:
        try:
            fid    = str(fmt.get("format_id", ""))
            ext    = str(fmt.get("ext", "mp4")).lower()
            vcodec = str(fmt.get("vcodec", "none"))
            acodec = str(fmt.get("acodec", "none"))
            dl_url = fmt.get("url", "")

            if not dl_url or ext in ("mhtml", "none", "webp"):
                continue

            height      = fmt.get("height") or 0
            tbr         = fmt.get("tbr")
            abr         = fmt.get("abr")
            fps         = fmt.get("fps")
            filesize    = fmt.get("filesize") or fmt.get("filesize_approx")
            format_note = str(fmt.get("format_note", ""))

            resolution = f"{height}p" if height else (None if vcodec == "none" else "Unknown")

            if resolution and vcodec != "none":
                key = (resolution, ext)
                if key in seen:
                    continue
                seen.add(key)

            no_watermark = False
            if _tiktok:
                no_watermark = (
                    "no watermark" in format_note.lower()
                    or "nowm" in fid.lower()
                    or fid.endswith("-1")
                )

            processed.append({
                "format_id":   fid,
                "ext":         ext,
                "resolution":  resolution,
                "filesize":    int(filesize) if filesize else None,
                "format_note": format_note or None,
                "vcodec":      vcodec,
                "acodec":      acodec,
                "url":         dl_url,
                "no_watermark": no_watermark,
                "tbr":         float(tbr) if tbr else None,
                "fps":         float(fps) if fps else None,
                "abr":         float(abr) if abr else None,
            })
        except Exception as e:
            logger.warning(f"Format skip: {e}")
            continue

    processed.sort(key=lambda f: (not f["no_watermark"], -(f["tbr"] or 0)))
    return processed[:30]


# ============================================================
# ENDPOINTS
# ============================================================
@app.get("/health")
async def health():
    try:
        import yt_dlp.version as v
        yt_version = v.__version__
    except Exception:
        yt_version = "unknown"
    return {"status": "ok", "yt_dlp_version": yt_version, "timestamp": time.time()}


@app.post("/extract")
async def extract(req: ExtractRequest, request: Request):
    url = req.url
    logger.info(f"Extract: {url[:80]}")

    ydl_opts = build_ydl_opts(url)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

    except yt_dlp.utils.DownloadError as e:
        err_str = str(e).lower()
        logger.warning(f"DownloadError: {e}")
        if "private" in err_str or "login" in err_str:
            raise HTTPException(403, "This video is private or requires login.")
        elif "not found" in err_str or "unavailable" in err_str:
            raise HTTPException(404, "Video not found or removed.")
        elif "unsupported" in err_str:
            raise HTTPException(422, "This website is not supported.")
        else:
            raise HTTPException(422, f"Could not extract video: {str(e)[:200]}")

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        raise HTTPException(500, "Internal server error.")

    if not info:
        raise HTTPException(422, "Could not retrieve video information.")

    # Handle playlists
    if info.get("_type") == "playlist":
        entries = info.get("entries", [])
        if not entries:
            raise HTTPException(422, "Playlist is empty.")
        info = entries[0]

    formats  = process_formats(info, url)
    platform = detect_platform(url)

    return JSONResponse(content={
        "title":       info.get("title", "Unknown Video"),
        "thumbnail":   info.get("thumbnail"),
        "platform":    platform,
        "duration":    format_duration(info.get("duration")),
        "webpage_url": info.get("webpage_url", url),
        "formats":     formats,
    })


@app.exception_handler(Exception)
async def global_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Server error."})


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
