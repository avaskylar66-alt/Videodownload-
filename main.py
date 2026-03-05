"""
SnapLoad Railway Backend — main.py
FastAPI + yt-dlp universal video extractor.

Features:
- POST /extract  — Extract video formats/URLs from any yt-dlp supported site
- GET  /health   — Health check endpoint
- TikTok no-watermark support via yt-dlp extractor args
- CORS configured for WordPress frontend
- Robust error handling & logging
"""

import logging
import os
import re
import time
from typing import Optional
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl, field_validator

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("snapload")

# ============================================================
# APP INIT
# ============================================================
app = FastAPI(
    title="SnapLoad Video Extractor API",
    description="Universal video downloader backend powered by yt-dlp. Supports 1000+ websites.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)

# ============================================================
# CORS MIDDLEWARE
# Allow your WordPress domain. Set ALLOWED_ORIGINS env var in Railway,
# e.g.: "https://yourdomain.com,https://www.yourdomain.com"
# Defaults to "*" for development/testing.
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
    allow_credentials=allowed_origins != ["*"],  # credentials require explicit origins
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

# ============================================================
# REQUEST / RESPONSE MODELS
# ============================================================
class ExtractRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("URL cannot be empty.")
        # Basic URL validation
        try:
            parsed = urlparse(v)
            if parsed.scheme not in ("http", "https"):
                raise ValueError("URL must use http or https.")
            if not parsed.netloc:
                raise ValueError("Invalid URL format.")
        except Exception as e:
            raise ValueError(f"Invalid URL: {e}")
        # Block local/private addresses
        blocked = ["localhost", "127.0.0.1", "0.0.0.0", "192.168.", "10.", "172."]
        for b in blocked:
            if b in parsed.netloc:
                raise ValueError("URL not allowed.")
        return v


class FormatInfo(BaseModel):
    format_id: str
    ext: str
    resolution: Optional[str]
    filesize: Optional[int]
    format_note: Optional[str]
    vcodec: str
    acodec: str
    url: Optional[str]
    no_watermark: bool
    tbr: Optional[float]
    fps: Optional[float]
    abr: Optional[float]


class ExtractResponse(BaseModel):
    title: str
    thumbnail: Optional[str]
    platform: str
    duration: Optional[str]
    webpage_url: Optional[str]
    formats: list[FormatInfo]


# ============================================================
# YT-DLP CONFIGURATION BUILDER
# ============================================================

def is_tiktok(url: str) -> bool:
    return "tiktok.com" in url.lower()


def build_ydl_opts(url: str) -> dict:
    """
    Build yt-dlp options. Includes TikTok-specific no-watermark config.
    """
    opts = {
        # Don't download anything — just extract info
        "skip_download":    True,
        "quiet":            True,
        "no_warnings":      True,

        # Extract ALL formats (not just best)
        "listformats":      False,
        "extract_flat":     False,

        # Cookies (set COOKIES_FILE env var to path of cookies.txt for
        # authenticated sites like Instagram, Twitter, etc.)
        **(
            {"cookiefile": os.getenv("COOKIES_FILE")}
            if os.getenv("COOKIES_FILE") else {}
        ),

        # Proxy support (set HTTP_PROXY env var in Railway for IP rotation)
        **(
            {"proxy": os.getenv("HTTP_PROXY")}
            if os.getenv("HTTP_PROXY") else {}
        ),

        # Rate limiting to avoid bans
        "sleep_interval":       0,
        "max_sleep_interval":   2,
        "sleep_interval_requests": 1,

        # User agent rotation — mimics a real browser
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),

        # Geo-bypass attempts
        "geo_bypass":           True,
        "geo_bypass_country":   "US",

        # Timeout
        "socket_timeout":       20,

        # Retries
        "retries":              3,
        "fragment_retries":     3,
    }

    # ============================================================
    # TIKTOK — No-Watermark Configuration
    # yt-dlp fetches the watermarked version by default.
    # These extractor_args target TikTok's API directly to get
    # the "play_addr" (no watermark) vs "download_addr" (watermarked).
    # ============================================================
    if is_tiktok(url):
        opts["extractor_args"] = {
            "tiktok": {
                # Request the no-watermark download address
                "app_name":     ["trill"],
                "app_version":  ["26.1.3"],
                # Force API version that returns no-wm links
                "api_hostname": ["api22-normal-c-alisg.tiktokv.com"],
            }
        }
        logger.info("TikTok URL detected — enabling no-watermark extraction")

    return opts


# ============================================================
# FORMAT PROCESSOR
# ============================================================

def process_formats(info: dict, url: str) -> list[dict]:
    """
    Process yt-dlp format list into clean, structured output.
    - Separates video, audio, and combined formats
    - Marks TikTok no-watermark formats
    - Deduplicates by resolution
    - Adds human-readable labels
    """
    raw_formats = info.get("formats", [])
    if not raw_formats:
        # Single-format video (direct URL in info dict)
        raw_formats = [{
            "format_id": "default",
            "ext":       info.get("ext", "mp4"),
            "url":       info.get("url", ""),
            "vcodec":    info.get("vcodec", "unknown"),
            "acodec":    info.get("acodec", "unknown"),
            "tbr":       info.get("tbr"),
            "filesize":  info.get("filesize"),
        }]

    processed = []
    seen_resolutions = set()
    _tiktok = is_tiktok(url)

    for fmt in raw_formats:
        try:
            fid     = str(fmt.get("format_id", ""))
            ext     = str(fmt.get("ext", "mp4")).lower()
            vcodec  = str(fmt.get("vcodec", "none"))
            acodec  = str(fmt.get("acodec", "none"))
            dl_url  = fmt.get("url", "")

            # Skip manifests, m3u8 index files without direct URLs
            if not dl_url or ext in ("mhtml", "none", "webp"):
                continue
            if "manifest" in str(fmt.get("format_note", "")).lower():
                continue
            # Skip DASH audio-only segments if we also have combined
            if ext == "m4a" and vcodec == "none" and not _tiktok:
                # Keep only best audio
                pass

            height      = fmt.get("height") or 0
            width       = fmt.get("width") or 0
            tbr         = fmt.get("tbr")
            abr         = fmt.get("abr")
            fps         = fmt.get("fps")
            filesize    = fmt.get("filesize") or fmt.get("filesize_approx")
            format_note = str(fmt.get("format_note", ""))

            # Build resolution string
            if height and width:
                resolution = f"{height}p"
            elif format_note and any(c.isdigit() for c in format_note):
                resolution = format_note
            elif vcodec == "none":
                resolution = None  # audio only
            else:
                resolution = "Unknown"

            # Deduplicate video formats by resolution (keep highest tbr)
            if resolution and vcodec != "none":
                key = (resolution, ext)
                if key in seen_resolutions:
                    continue
                seen_resolutions.add(key)

            # =============================================
            # TIKTOK NO-WATERMARK DETECTION
            # yt-dlp marks the clean version differently:
            # format_note contains "no watermark" or "aweme" in format_id
            # =============================================
            no_watermark = False
            if _tiktok:
                no_watermark = (
                    "no watermark" in format_note.lower()
                    or "nowm" in fid.lower()
                    or fid.endswith("-1")  # yt-dlp TikTok index 1 = no watermark
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
            logger.warning(f"Skipping format {fmt.get('format_id')}: {e}")
            continue

    # Sort: no_watermark first, then by tbr descending
    processed.sort(key=lambda f: (not f["no_watermark"], -(f["tbr"] or 0)))

    return processed[:30]  # Return max 30 formats


def detect_platform(url: str) -> str:
    """Detect the platform name from URL."""
    u = url.lower()
    platforms = {
        "youtube.com":      "YouTube",
        "youtu.be":         "YouTube",
        "tiktok.com":       "TikTok",
        "instagram.com":    "Instagram",
        "facebook.com":     "Facebook",
        "fb.watch":         "Facebook",
        "twitter.com":      "Twitter/X",
        "x.com":            "Twitter/X",
        "reddit.com":       "Reddit",
        "vimeo.com":        "Vimeo",
        "twitch.tv":        "Twitch",
        "soundcloud.com":   "SoundCloud",
        "dailymotion.com":  "Dailymotion",
        "pinterest.com":    "Pinterest",
        "linkedin.com":     "LinkedIn",
        "snapchat.com":     "Snapchat",
    }
    for domain, name in platforms.items():
        if domain in u:
            return name

    # Generic: extract domain as platform name
    try:
        netloc = urlparse(url).netloc.replace("www.", "")
        return netloc.split(".")[0].capitalize()
    except Exception:
        return "Unknown"


# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/health")
async def health():
    """Health check + version info."""
    yt_dlp_version = "unknown"
    try:
        import yt_dlp.version as v
        yt_dlp_version = v.__version__
    except Exception:
        pass
    return {
        "status":           "ok",
        "service":          "SnapLoad Video Extractor",
        "yt_dlp_version":   yt_dlp_version,
        "timestamp":        time.time(),
    }


@app.post("/extract", response_model=ExtractResponse)
async def extract(req: ExtractRequest, request: Request):
    """
    Main extraction endpoint.
    Accepts a video URL and returns available formats, metadata, and direct download URLs.
    """
    url = req.url
    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"Extract request | url={url[:80]} | ip={client_ip}")

    ydl_opts = build_ydl_opts(url)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

    except yt_dlp.utils.DownloadError as e:
        err_str = str(e).lower()
        logger.warning(f"yt-dlp DownloadError for {url}: {e}")

        if "private" in err_str or "login" in err_str or "sign in" in err_str:
            raise HTTPException(
                status_code=403,
                detail="This video is private or requires login. We can't access it."
            )
        elif "not found" in err_str or "no video" in err_str or "unavailable" in err_str:
            raise HTTPException(
                status_code=404,
                detail="Video not found or has been removed."
            )
        elif "geo" in err_str or "region" in err_str:
            raise HTTPException(
                status_code=451,
                detail="This video is not available in our server's region."
            )
        elif "unsupported" in err_str:
            raise HTTPException(
                status_code=422,
                detail="This website is not supported by our downloader."
            )
        else:
            raise HTTPException(
                status_code=422,
                detail=f"Could not extract video: {str(e)[:200]}"
            )

    except yt_dlp.utils.ExtractorError as e:
        logger.warning(f"yt-dlp ExtractorError for {url}: {e}")
        raise HTTPException(
            status_code=422,
            detail="Failed to extract video information. The page may have changed."
        )

    except Exception as e:
        logger.error(f"Unexpected error for {url}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again."
        )

    if not info:
        raise HTTPException(
            status_code=422,
            detail="Could not retrieve video information."
        )

    # Handle playlists — take first item
    if info.get("_type") == "playlist":
        entries = info.get("entries", [])
        if not entries:
            raise HTTPException(status_code=422, detail="Playlist is empty.")
        info = entries[0]
        if info is None:
            raise HTTPException(status_code=422, detail="Could not load first playlist item.")
        logger.info("Playlist detected — using first entry")

    # Process formats
    formats = process_formats(info, url)
    platform = detect_platform(url)

    # Duration formatting
    duration_raw = info.get("duration")
    duration_str = None
    if duration_raw:
        try:
            total_seconds = int(float(duration_raw))
            minutes = total_seconds // 60
            seconds = total_seconds % 60
            duration_str = f"{minutes}:{seconds:02d}"
        except Exception:
            duration_str = str(duration_raw)

    response_data = {
        "title":       info.get("title", "Unknown Video"),
        "thumbnail":   info.get("thumbnail"),
        "platform":    platform,
        "duration":    duration_str,
        "webpage_url": info.get("webpage_url", url),
        "formats":     formats,
    }

    logger.info(
        f"Extracted: '{response_data['title'][:50]}' | "
        f"{len(formats)} formats | platform={platform}"
    )

    return JSONResponse(content=response_data)


# ============================================================
# GLOBAL EXCEPTION HANDLER
# ============================================================
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected server error occurred."}
    )


# ============================================================
# ENTRY POINT (local dev)
# ============================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True, log_level="info")
