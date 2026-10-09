#!/usr/bin/env python3
"""Refresh place details from OpenStreetMap and rebuild the app.

    python3 tools/update.py              refresh address, website, hours, then build
    python3 tools/update.py --build-only rebuild from data/places.json

Builds index.html (the website) and dist/artifact.html (the same app without
the page wrapper, for publishing as a Claude artifact).

Each place in data/places.json may carry:
    osm        OpenStreetMap id (N/W/R + number), set by tools/match_osm.py
    ad, web    address and website
    hours      opening hours in OpenStreetMap syntax, e.g. "Mo-Fr 08:00-17:00; Sa 09:00-13:00"
    ad_src, web_src, hours_src   "manual" keeps a hand-entered value from being overwritten
Changes found by a refresh are printed and appended to data/changes.log.
"""
import datetime, json, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACES = ROOT / "data" / "places.json"
TEMPLATE = ROOT / "src" / "template.html"
OUT = ROOT / "index.html"
ARTIFACT_OUT = ROOT / "dist" / "artifact.html"
PAGE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="Where to eat, stay and visit in eight Washington cities, with prices, hours and one-tap directions.">
<meta name="theme-color" content="#0A6A4E">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="WA Guide">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="icon.svg">
<style>:root{padding:env(safe-area-inset-top,0px) 0 env(safe-area-inset-bottom,0px)}body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>
"""
LOG = ROOT / "data" / "changes.log"
UA = "WashingtonCityTourGuide/1.0 (+https://github.com/laminedo/citytourguide)"
DAYS = ["mo", "tu", "we", "th", "fr", "sa", "su"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
DAY_RE = r"(?:mo|tu|we|th|fr|sa|su|ph|sh)"


def all_places(data):
    for c in data["cities"]:
        for sec in c["sections"].values():
            yield from sec


# ---------- opening hours ----------

def _days(spec):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part or part in ("ph", "sh"):
            continue
        if "-" in part:
            a, b = [x.strip() for x in part.split("-", 1)]
            if a not in DAYS or b not in DAYS:
                return None
            i, j = DAYS.index(a), DAYS.index(b)
            out += [k % 7 for k in range(i, i + ((j - i) % 7) + 1)]
        elif part in DAYS:
            out.append(DAYS.index(part))
        else:
            return None
    return out


def _month_applies(spec, today):
    """spec like 'may-sep' or 'nov-feb' or 'jul'. None when it is more complex than that."""
    m = re.fullmatch(r"([a-z]{3})(?:\s*-\s*([a-z]{3}))?", spec.strip())
    if not m or m.group(1) not in MONTHS or (m.group(2) and m.group(2) not in MONTHS):
        return None
    a = MONTHS.index(m.group(1))
    b = MONTHS.index(m.group(2)) if m.group(2) else a
    cur = today.month - 1
    return a <= cur <= b if a <= b else (cur >= a or cur <= b)


def parse_hours(raw, today=None):
    """Turn an OpenStreetMap opening_hours string into a weekly table.

    Returns (week, note). week is seven lists (Monday first) of [open, close]
    minutes, or None when the string cannot be reduced to a weekly table.
    """
    today = today or datetime.date.today()
    s = re.sub(r'"[^"]*"', "", raw).strip().lower()
    if not s:
        return None, None
    if s in ("24/7", "mo-su 00:00-24:00", "00:00-24:00"):
        return [[[0, 1440]] for _ in range(7)], None
    # "Mo 10:00-12:00, We 10:00-12:00" is two rules; "10:00-12:00,13:00-17:00" is one.
    s = re.sub(r"(?<=[\d+]),\s*(?=" + DAY_RE + r"\b)", ";", s)
    s = re.sub(r"(?<=off),\s*(?=" + DAY_RE + r"\b)", ";", s)
    week = [[] for _ in range(7)]
    note, got = None, False
    for rule in re.split(r";|\|\|", s):
        rule = rule.strip()
        if not rule:
            continue
        m = re.match(r"^((?:[a-z]{3}(?:\s+\d{1,2})?(?:\s*-\s*(?:[a-z]{3}\s*)?\d{0,2})?\s*,?\s*)+?)(?::|\s)\s*(?=(?:" + DAY_RE + r"\b|\d|off|closed))", rule)
        if m and m.group(1).strip()[:3] in MONTHS:
            sel, rest = m.group(1).strip().rstrip(":"), rule[m.end():].strip()
            if rest in ("off", "closed"):
                note = "Closed on some dates. Check the website."
                continue
            applies = _month_applies(sel, today)
            if applies is None:
                return None, None
            note = "Hours change with the season."
            if not applies:
                continue
            rule = rest
        elif rule[:3] in MONTHS or re.match(r"^(easter|week)\b", rule):
            if rule.endswith("off") or rule.endswith("closed"):
                note = "Closed on some dates. Check the website."
                continue
            return None, None
        m = re.match(r"^((?:" + DAY_RE + r"\b[\s,\-]*)+)?(.*)$", rule)
        dayspec, times = (m.group(1) or "").strip().rstrip(","), m.group(2).strip()
        if dayspec and all(t in ("ph", "sh") for t in re.split(r"[\s,]+", dayspec) if t):
            continue  # holiday-only rule
        days = _days(dayspec) if dayspec else list(range(7))
        if days is None:
            return None, None
        if times in ("off", "closed"):
            for d in days:
                week[d] = []
            got = True
            continue
        if not times and dayspec:
            intervals = [[0, 1440]]
        else:
            intervals = []
            for t in times.split(","):
                t = t.strip()
                mt = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\+?", t)
                if mt:
                    o = int(mt.group(1)) * 60 + int(mt.group(2))
                    c = int(mt.group(3)) * 60 + int(mt.group(4))
                    if c <= o:
                        c += 1440
                    intervals.append([o, c])
                    continue
                mt = re.fullmatch(r"(\d{1,2}):(\d{2})\+", t)
                if mt:
                    intervals.append([int(mt.group(1)) * 60 + int(mt.group(2)), 1440])
                    note = "Closing time varies."
                    continue
                return None, None
        for d in days:
            week[d] = [list(x) for x in intervals]
        got = True
    if not got or not any(week):
        return None, None
    return week, note


def hours_text(raw):
    """Readable fallback for hours that do not fit a weekly table."""
    s = raw.lower()
    if ("sunrise" in s or "dawn" in s) and ("sunset" in s or "dusk" in s):
        return "Sunrise to sunset, daily"
    return "Vary by date. Check the website."


# ---------- OpenStreetMap refresh ----------

def lookup(ids):
    url = "https://nominatim.openstreetmap.org/lookup?" + urllib.parse.urlencode({
        "osm_ids": ",".join(ids), "format": "jsonv2", "addressdetails": 1, "extratags": 1})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            print(f"  lookup failed ({e}); retrying", file=sys.stderr)
            time.sleep(5 + attempt * 10)
    return None


def format_address(a):
    if not a:
        return None
    town = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("municipality")
    street = " ".join(x for x in (a.get("house_number"), a.get("road")) if x) if a.get("road") else ""
    tail = " ".join(x for x in ("WA", a.get("postcode")) if x)
    # A town alone is not worth showing; the card already names the city.
    return ", ".join(x for x in (street, town, tail) if x) if street else None


def clean_url(u):
    if not u:
        return None
    u = u.strip().split(";")[0].strip()
    if not re.match(r"^https?://", u):
        u = "https://" + u
    return u


def refresh(data):
    places = [p for p in all_places(data) if p.get("osm")]
    by_osm = {}
    for p in places:
        by_osm.setdefault(p["osm"], []).append(p)
    ids = sorted(by_osm)
    changes, failed = [], 0
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        res = lookup(chunk)
        time.sleep(1.5)
        if res is None:
            failed += len(chunk)
            continue
        for r in res:
            key = r["osm_type"][0].upper() + str(r["osm_id"])
            tags = r.get("extratags") or {}
            new = {
                "ad": format_address(r.get("address")),
                "web": clean_url(tags.get("website") or tags.get("contact:website") or tags.get("url")),
                "hours": tags.get("opening_hours"),
            }
            for p in by_osm.get(key, []):
                for field, value in new.items():
                    if p.get(field + "_src") == "manual" or not value or p.get(field) == value:
                        continue
                    if p.get(field):
                        changes.append(f'{p["n"]} ({p["id"]}): {field} "{p[field]}" -> "{value}"')
                    else:
                        changes.append(f'{p["n"]} ({p["id"]}): {field} added "{value}"')
                    p[field] = value
    return changes, failed


# ---------- build ----------

def build(data):
    today = datetime.date.today()
    out = {"updated": data["updated"], "cities": []}
    stats = {"places": 0, "ad": 0, "web": 0, "hours": 0}
    for c in data["cities"]:
        city = {k: c[k] for k in ("id", "name", "mi", "drive", "blurb")}
        city["sections"] = {}
        for sec, items in c["sections"].items():
            rows = []
            for p in items:
                row = {k: p[k] for k in ("id", "n", "a", "d", "l", "pr", "t")}
                for k in ("q", "top", "ok", "ad", "web"):
                    if p.get(k):
                        row[k] = p[k]
                if p.get("hours"):
                    week, note = parse_hours(p["hours"], today)
                    if week:
                        row["h"] = week
                        if note:
                            row["hn"] = note
                    else:
                        text = hours_text(p["hours"])
                        if text:
                            row["hx"] = text
                stats["places"] += 1
                stats["ad"] += "ad" in row
                stats["web"] += "web" in row
                stats["hours"] += "h" in row or "hx" in row
                rows.append(row)
            city["sections"][sec] = rows
        out["cities"].append(city)
    blob = json.dumps(out, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text()
    marker = "/*__PLACES__*/null"
    if marker not in html:
        sys.exit("template is missing the data marker")
    app = html.replace(marker, blob)
    ARTIFACT_OUT.parent.mkdir(exist_ok=True)
    ARTIFACT_OUT.write_text(app)
    head, body = app.split("</style>", 1)
    OUT.write_text(PAGE_HEAD + head + "</style>\n</head>\n<body>" + body + "</body>\n</html>\n")
    return stats


def main():
    data = json.loads(PLACES.read_text())
    if "--build-only" not in sys.argv:
        changes, failed = refresh(data)
        stamp = datetime.date.today().isoformat()
        if failed:
            print(f"WARNING: could not reach OpenStreetMap for {failed} places; their details were left as they were.")
        else:
            data["updated"] = stamp
        PLACES.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        if changes:
            with LOG.open("a") as f:
                f.write(f"\n== {stamp} ==\n" + "\n".join(changes) + "\n")
        print(f"{len(changes)} change(s) from OpenStreetMap")
        for line in changes:
            print("  " + line)
    stats = build(data)
    print(f'Built index.html and dist/artifact.html: {stats["places"]} places, {stats["ad"]} with address, {stats["web"]} with website, {stats["hours"]} with hours')


if __name__ == "__main__":
    main()
