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
from datetime import datetime, timedelta, time as dt_time, timezone

import pytz
import requests
from lxml import etree

# ── Configuration ─────────────────────────────────────────────────────────────

M3U_SOURCE         = "https://www.apsattv.com/rakutentv-uk.m3u"
M3U_HASH_FILE       = ".m3u_source_hash"   # tracks last-seen content hash across runs
TIMEZONE           = pytz.timezone("Europe/London")
DT_FORMAT          = "%Y%m%d%H%M%S %z"
GAP_THRESHOLD_SECS = 60  # snap end-times within this many seconds of the next start

RETRY_ATTEMPTS     = 6
RETRY_BACKOFF_SECS = 30


# ── Helpers ───────────────────────────────────────────────────────────────────

def remove_control_characters(s: str) -> str:
    """Strip Unicode control characters from a string."""
    return "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")


def normalize(name: str) -> str:
    """Lowercase + keep only alphanumerics for fuzzy channel matching."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def to_tz_str(val) -> str:
    """Convert an epoch int/float or a datetime to a localised XMLTV timestamp."""
    if isinstance(val, datetime):
        dt = val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    else:
        dt = datetime.fromtimestamp(val, tz=timezone.utc)
    return dt.astimezone(TIMEZONE).strftime(DT_FORMAT)


def fetch_with_retry(url: str, timeout: int = 30) -> requests.Response:
    """
    GET a URL with automatic retry on 503 / connection errors.
    Raises the final exception if all attempts fail.
    """
    last_exc = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 503 and attempt < RETRY_ATTEMPTS:
                print(f"  [attempt {attempt}/{RETRY_ATTEMPTS}] 503 received, "
                      f"retrying in {RETRY_BACKOFF_SECS}s ...")
                time.sleep(RETRY_BACKOFF_SECS)
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < RETRY_ATTEMPTS:
                print(f"  [attempt {attempt}/{RETRY_ATTEMPTS}] Request error: {exc}, "
                      f"retrying in {RETRY_BACKOFF_SECS}s ...")
                time.sleep(RETRY_BACKOFF_SECS)
    raise last_exc


# ── EPG window ────────────────────────────────────────────────────────────────

def get_epg_window():
    """Return (start, end) datetimes for a 72-hour EPG window starting now."""
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    end = datetime.combine(datetime.now().date(), dt_time(0, 0)) + timedelta(days=3)
    return now, end


def check_m3u_freshness(text: str) -> None:
    """
    Compare a hash of the fetched M3U body against the hash stored from the
    previous run (committed alongside epg.xml/playlist.m3u in the repo).
    Logs whether apsattv's list actually changed since last run — apsattv
    updates this list manually/irregularly, so this is just a freshness
    signal in the Actions log, not a hard gate.
    """
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
    """
    Download and parse an M3U playlist.

    Returns two lookup dicts:
      by_name  — keyed by normalize(display_name)
      by_slug  — keyed by the portion of tvg-id after 'RakutenTV-UK_'
    Each value: { tvg_id, tvg_logo, group, name, url }
    """
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
    """
    Find the M3U entry for a Rakuten API channel.
    Strategy order:
      1. Exact normalised display-name match
      2. tvg-id slug match
      3. Substring match (last resort)
    """
    norm = normalize(ch_name)

    if norm in by_name:
        return by_name[norm]
    if ch_id.lower() in by_slug:
        return by_slug[ch_id.lower()]
    for key, entry in by_name.items():
        if norm in key or key in norm:
            return entry

    return None


# ── XMLTV builder ─────────────────────────────────────────────────────────────

def build_xmltv(channels: list, programmes: list) -> bytes:
    """Serialise channels + programmes to a well-formed XMLTV byte string."""
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


# ── M3U builder ───────────────────────────────────────────────────────────────

EPG_URL = "https://raw.githubusercontent.com/BuddyChewChew/RakutenTV/main/epg.xml"


def build_m3u(channels: list) -> str:
    """Build an M3U playlist; skips channels without a matched stream URL."""
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # 1. Fetch & parse M3U stream source
    m3u_by_name, m3u_by_slug = fetch_m3u(M3U_SOURCE)

    # 2. Build Rakuten EPG API URL
    epg_start, epg_end = get_epg_window()

    params = (
        "classification_id=18"
        "&device_identifier=web"
        "&device_stream_audio_quality=2.0"
        "&device_stream_hdr_type=NONE"
        "&device_stream_video_quality=FHD"
        "&epg_duration_minutes=360"
        f"&epg_ends_at={epg_end.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
        f"&epg_ends_at_timestamp={epg_end.timestamp()}"
        f"&epg_starts_at={epg_start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
        f"&epg_starts_at_timestamp={epg_start.timestamp()}"
        "&locale=en"
        "&market_code=uk"
        "&per_page=250"
    )
    api_url = "https://gizmo.rakuten.tv/v3/live_channels?" + params.replace(":", "%3A")

    print("\nFetching EPG data from Rakuten API ...")
    resp = fetch_with_retry(api_url)

    data = resp.json()["data"]
    print(f"Retrieved {len(data)} channels\n")

    channels_data  = []
    programme_data = []

    for channel in data:
        ch_name = channel["title"]
        ch_id   = channel["id"]
        print(f"  {ch_name}")

        # Logo
        ch_icon = None
        if channel.get("images"):
            imgs    = channel["images"]
            ch_icon = imgs.get("artwork_negative") or imgs.get("artwork")

        # Language & tags
        ch_language = ch_tags = None
        if channel.get("labels"):
            labels = channel["labels"]
            langs  = labels.get("languages")
            if langs:
                ch_language = langs[0].get("id")
            ch_tags = labels.get("tags")

        # Match to an M3U stream entry (used only for its stream URL — the
        # apsattv tvg-id format is inconsistent across mirrors/refreshes and
        # is NOT used as our channel identifier, see note below)
        m3u = match_m3u(ch_name, ch_id, m3u_by_name, m3u_by_slug)

        channels_data.append({
            "name":       ch_name,
            "epg_number": channel.get("channel_number"),
            "id":         ch_id,
            "icon":       ch_icon,
            "language":   ch_language,
            "tags":       ch_tags,
            "stream_url": m3u["url"] if m3u else None,
            # IMPORTANT: always use Rakuten's own slug (e.g. "mr-bean-live-action")
            # as tvg-id, NOT the source M3U's tvg-id. epg.xml's <channel id="...">
            # is built from this same ch_id, so playlist.m3u and epg.xml must
            # share this value or TiviMate can't match programmes to channels.
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

    # 3. Normalise programme end-times (close small gaps / remove overlaps)
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

    # 4. Write outputs
    with open("epg.xml", "wb") as f:
        f.write(build_xmltv(channels_data, programme_data))
    print("\nWrote epg.xml")

    with open("playlist.m3u", "w", encoding="utf-8") as f:
        f.write(build_m3u(channels_data))
    print("Wrote playlist.m3u")


if __name__ == "__main__":
    main()
