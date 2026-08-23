#!/usr/bin/env python3
"""
RakutenTV UK — EPG + M3U generator
Fetches programme data from the Rakuten v3/live_channels API and merges
stream URLs from an external M3U source to produce:
  • epg.xml      — 72-hour XMLTV guide
  • playlist.m3u — paired M3U playlist (channels with matched streams only)
"""

import hashlib
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytz
import requests
from lxml import etree

# ── Configuration ─────────────────────────────────────────────────────────────

M3U_SOURCE         = "https://www.apsattv.com/rakutentv-uk.m3u"
M3U_HASH_FILE      = ".m3u_source_hash"
TIMEZONE           = pytz.timezone("Europe/London")
DT_FORMAT          = "%Y%m%d%H%M%S %z"
GAP_THRESHOLD_SECS = 60

RETRY_ATTEMPTS     = 4
RETRY_BACKOFF_SECS = 20

API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Origin": "https://rakuten.tv",
    "Referer": "https://rakuten.tv/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Values that have historically worked. 250 is now rejected.
PER_PAGE_CANDIDATES = [100, 50, 20]


# ── Helpers ───────────────────────────────────────────────────────────────────

def remove_control_characters(s: str) -> str:
    return "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def to_tz_str(val) -> str:
    if isinstance(val, datetime):
        dt = val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    else:
        dt = datetime.fromtimestamp(val, tz=timezone.utc)
    return dt.astimezone(TIMEZONE).strftime(DT_FORMAT)


def fetch_with_retry(url: str, headers: dict | None = None, timeout: int = 30) -> requests.Response:
    last_exc = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=headers or {}, timeout=timeout)
            if resp.status_code == 503 and attempt < RETRY_ATTEMPTS:
                print(f"  [attempt {attempt}/{RETRY_ATTEMPTS}] 503, retrying in {RETRY_BACKOFF_SECS}s ...")
                time.sleep(RETRY_BACKOFF_SECS)
                continue
            if 400 <= resp.status_code < 500:
                print(f"  HTTP {resp.status_code} body: {resp.text[:600]!r}")
                resp.raise_for_status()
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            # Don't keep retrying pure 4xx
            if hasattr(exc, "response") and exc.response is not None and 400 <= exc.response.status_code < 500:
                break
            if attempt < RETRY_ATTEMPTS:
                print(f"  [attempt {attempt}/{RETRY_ATTEMPTS}] {exc}, retrying in {RETRY_BACKOFF_SECS}s ...")
                time.sleep(RETRY_BACKOFF_SECS)
    raise last_exc


# ── EPG window ────────────────────────────────────────────────────────────────

def get_epg_window(hours: int = 72):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = (now + timedelta(hours=hours)).replace(hour=0, minute=0, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    return now, end


def check_m3u_freshness(text: str) -> None:
    current_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    previous_hash = None
    try:
        with open(M3U_HASH_FILE, "r") as f:
            previous_hash = f.read().strip()
    except FileNotFoundError:
        pass

    if previous_hash is None:
        print("  [freshness] no previous hash on record — treating as baseline")
    elif current_hash == previous_hash:
        print("  [freshness] M3U source UNCHANGED since last run")
    else:
        print("  [freshness] M3U source CHANGED since last run")

    with open(M3U_HASH_FILE, "w") as f:
        f.write(current_hash)


# ── M3U fetching & parsing ────────────────────────────────────────────────────

def fetch_m3u(url: str):
    print(f"Fetching M3U: {url}")
    resp = fetch_with_retry(url)
    check_m3u_freshness(resp.text)

    by_name = {}
    by_slug = {}
    lines = resp.text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            tvg_id_m   = re.search(r'tvg-id="([^"]*)"',      line)
            tvg_logo_m = re.search(r'tvg-logo="([^"]*)"',    line)
            group_m    = re.search(r'group-title="([^"]*)"', line)

            tvg_id   = tvg_id_m.group(1)   if tvg_id_m   else ""
            tvg_logo = tvg_logo_m.group(1) if tvg_logo_m else ""
            group    = group_m.group(1)    if group_m    else "RakutenTV UK"

            display_name = line.rsplit(",", 1)[-1].strip()

            stream_url = ""
            i += 1
            while i < len(lines):
                candidate = lines[i].strip()
                if candidate and not candidate.startswith("#"):
                    stream_url = candidate
                    break
                i += 1

            entry = {
                "tvg_id":   tvg_id,
                "tvg_logo": tvg_logo,
                "group":    group,
                "name":     display_name,
                "url":      stream_url,
            }

            by_name[normalize(display_name)] = entry

            slug_m = re.search(r"RakutenTV-UK_(.+)$", tvg_id)
            if slug_m:
                by_slug[slug_m.group(1).lower()] = entry

        i += 1

    print(f"  -> parsed {len(by_name)} channels from M3U")
    return by_name, by_slug


def match_m3u(ch_name: str, ch_id: str, by_name: dict, by_slug: dict):
    norm = normalize(ch_name)

    if norm in by_name:
        return by_name[norm]
    if ch_id.lower() in by_slug:
        return by_slug[ch_id.lower()]
    for key, entry in by_name.items():
        if norm in key or key in norm:
            return entry
    return None


# ── XMLTV / M3U builders (unchanged) ──────────────────────────────────────────

def build_xmltv(channels: list, programmes: list) -> bytes:
    root = etree.Element("tv")
    root.set("generator-info-name", "rakuten-uk-epg")
    root.set("generator-info-url",  "https://github.com/BuddyChewChew/RakutenTV")

    for ch in channels:
        channel = etree.SubElement(root, "channel")
        channel.set("id", str(ch["id"]))

        display = etree.SubElement(channel, "display-name")
        lang = (ch.get("language") or "en").rstrip("s").lower()
        display.set("lang", lang)
        display.text = ch["name"]

        if ch.get("icon"):
            icon = etree.SubElement(channel, "icon")
            icon.set("src", ch["icon"])
            icon.text = ""

    for pr in programmes:
        prog = etree.SubElement(root, "programme")
        prog.set("channel", str(pr["channel_id"]))
        prog.set("start",   to_tz_str(pr["starts_at"]))
        prog.set("stop",    to_tz_str(pr["ends_at"]))

        title = etree.SubElement(prog, "title")
        title.set("lang", "en")
        title.text = pr["title"]

        if pr.get("subtitle"):
            sub = etree.SubElement(prog, "sub-title")
            sub.set("lang", "en")
            sub.text = remove_control_characters(pr["subtitle"])

        if pr.get("description"):
            desc = etree.SubElement(prog, "desc")
            desc.set("lang", "en")
            desc.text = remove_control_characters(pr["description"])

        if pr.get("tags"):
            for tag in pr["tags"]:
                cat = etree.SubElement(prog, "category")
                cat.set("lang", "en")
                cat.text = tag.get("name", "")

    return etree.tostring(root, pretty_print=True, encoding="utf-8")


EPG_URL = "https://raw.githubusercontent.com/BuddyChewChew/RakutenTV/main/epg.xml"


def build_m3u(channels: list) -> str:
    lines     = [f'#EXTM3U url-tvg="{EPG_URL}"']
    matched   = 0
    unmatched = []

    for ch in channels:
        url = ch.get("stream_url")
        if not url:
            unmatched.append(ch["name"])
            continue

        tvg_id   = ch.get("tvg_id")   or ch["id"]
        tvg_logo = ch.get("tvg_logo") or ch.get("icon") or ""
        group    = ch.get("group")    or "RakutenTV UK"

        lines.append(
            f'#EXTINF:-1 tvg-id="{tvg_id}" '
            f'tvg-logo="{tvg_logo}" '
            f'group-title="{group}",{ch["name"]}'
        )
        lines.append(url)
        matched += 1

    print(f"\nM3U: {matched} matched, {len(unmatched)} unmatched")
    if unmatched:
        print("Unmatched channels (no stream URL found):")
        for n in unmatched:
            print(f"  - {n}")

    return "\n".join(lines) + "\n"


# ── API helpers ───────────────────────────────────────────────────────────────

def build_api_url(epg_start, epg_end, per_page: int, page: int = 1,
                  include_timestamps: bool = True) -> str:
    params = {
        "classification_id": "18",
        "device_identifier": "web",
        "device_stream_audio_quality": "2.0",
        "device_stream_hdr_type": "NONE",
        "device_stream_video_quality": "FHD",
        "epg_duration_minutes": "360",
        "epg_ends_at": epg_end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "epg_starts_at": epg_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "locale": "en",
        "market_code": "uk",
        "per_page": str(per_page),
        "page": str(page),
    }
    if include_timestamps:
        params["epg_ends_at_timestamp"] = str(int(epg_end.timestamp()))
        params["epg_starts_at_timestamp"] = str(int(epg_start.timestamp()))

    return "https://gizmo.rakuten.tv/v3/live_channels?" + urlencode(params)


def fetch_one_page(epg_start, epg_end, per_page: int, page: int,
                   include_timestamps: bool) -> list:
    url = build_api_url(epg_start, epg_end, per_page, page, include_timestamps)
    resp = fetch_with_retry(url, headers=API_HEADERS)
    data = resp.json().get("data") or []
    return data


def fetch_epg_data() -> list:
    """
    Try different per_page values and window sizes until something works.
    Also paginates so we still get the full channel list.
    """
    strategies = [
        {"hours": 72, "timestamps": True,  "label": "72h + timestamps"},
        {"hours": 72, "timestamps": False, "label": "72h (dates only)"},
        {"hours": 48, "timestamps": True,  "label": "48h + timestamps"},
        {"hours": 24, "timestamps": False, "label": "24h (dates only)"},
    ]

    last_exc = None

    for strat in strategies:
        for per_page in PER_PAGE_CANDIDATES:
            epg_start, epg_end = get_epg_window(hours=strat["hours"])
            print(f"\nTrying: {strat['label']}, per_page={per_page}")
            print(f"  window: {epg_start.isoformat()} → {epg_end.isoformat()}")

            try:
                all_channels = []
                page = 1
                while True:
                    chunk = fetch_one_page(
                        epg_start, epg_end, per_page, page, strat["timestamps"]
                    )
                    if not chunk:
                        break
                    all_channels.extend(chunk)
                    print(f"  page {page}: +{len(chunk)} channels (total {len(all_channels)})")
                    # Stop when we receive fewer than requested (last page)
                    if len(chunk) < per_page:
                        break
                    page += 1
                    # Safety limit
                    if page > 10:
                        break

                if all_channels:
                    print(f"  ✓ Success — retrieved {len(all_channels)} channels")
                    return all_channels
                else:
                    print("  empty data array")
            except Exception as exc:
                last_exc = exc
                print(f"  ✗ Failed: {exc}")
                continue

    raise RuntimeError(
        "All EPG fetch strategies failed. Last error: " + str(last_exc)
    ) from last_exc


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    m3u_by_name, m3u_by_slug = fetch_m3u(M3U_SOURCE)

    print("\nFetching EPG data from Rakuten API ...")
    data = fetch_epg_data()
    print(f"\nRetrieved {len(data)} channels\n")

    channels_data  = []
    programme_data = []

    for channel in data:
        ch_name = channel["title"]
        ch_id   = channel["id"]
        print(f"  {ch_name}")

        ch_icon = None
        if channel.get("images"):
            imgs = channel["images"]
            ch_icon = imgs.get("artwork_negative") or imgs.get("artwork")

        ch_language = ch_tags = None
        if channel.get("labels"):
            labels = channel["labels"]
            langs  = labels.get("languages")
            if langs:
                ch_language = langs[0].get("id")
            ch_tags = labels.get("tags")

        m3u = match_m3u(ch_name, ch_id, m3u_by_name, m3u_by_slug)

        channels_data.append({
            "name":       ch_name,
            "epg_number": channel.get("channel_number"),
            "id":         ch_id,
            "icon":       ch_icon,
            "language":   ch_language,
            "tags":       ch_tags,
            "stream_url": m3u["url"] if m3u else None,
            "tvg_id":     ch_id,
            "tvg_logo":   m3u["tvg_logo"] if m3u else ch_icon,
            "group":      m3u["group"]    if m3u else "RakutenTV UK",
        })

        for item in channel.get("live_programs", []):
            programme_data.append({
                "title":       item["title"],
                "subtitle":    item.get("subtitle"),
                "description": item.get("description"),
                "starts_at":   datetime.strptime(item["starts_at"], "%Y-%m-%dT%H:%M:%S.000%z"),
                "ends_at":     datetime.strptime(item["ends_at"],   "%Y-%m-%dT%H:%M:%S.000%z"),
                "channel_id":  ch_id,
                "language":    ch_language,
                "tags":        ch_tags,
            })

    # Normalise end times
    programme_data.sort(key=lambda p: (p["channel_id"], p["starts_at"]))
    by_channel = {}
    for p in programme_data:
        by_channel.setdefault(p["channel_id"], []).append(p)

    for plist in by_channel.values():
        for i in range(len(plist) - 1):
            cur, nxt = plist[i], plist[i + 1]
            if nxt["starts_at"] <= cur["ends_at"]:
                cur["ends_at"] = nxt["starts_at"]
            elif (nxt["starts_at"] - cur["ends_at"]).total_seconds() <= GAP_THRESHOLD_SECS:
                cur["ends_at"] = nxt["starts_at"]

    with open("epg.xml", "wb") as f:
        f.write(build_xmltv(channels_data, programme_data))
    print("\nWrote epg.xml")

    with open("playlist.m3u", "w", encoding="utf-8") as f:
        f.write(build_m3u(channels_data))
    print("Wrote playlist.m3u")


if __name__ == "__main__":
    main()
