#!/usr/bin/env python3
"""Find each place in OpenStreetMap and record its OSM id.

    python3 tools/match_osm.py          search for places not looked up yet
    python3 tools/match_osm.py --apply  write confident matches into data/places.json

Run when places are added. tools/update.py then refreshes address, website
and hours by id. Raw search results go to data/osm_candidates.json for review.
"""
import json, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACES = ROOT / "data" / "places.json"
CANDIDATES = ROOT / "data" / "osm_candidates.json"
UA = "WashingtonCityTourGuide/1.0 (+https://github.com/laminedo/citytourguide)"
STOP = {"the", "a", "an", "of", "and", "at", "in", "by", "co", "company", "restaurant", "cafe", "hotel"}


def tokens(s):
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[’']", "", s)
    return [t for t in re.split(r"[^a-z0-9]+", s) if t and t not in STOP]


def search(q):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": q, "format": "jsonv2", "addressdetails": 1, "extratags": 1, "limit": 8, "countrycodes": "us"})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # network hiccup: wait and retry
            time.sleep(3 + attempt * 3)
    return []


def score(place_name, house, cand):
    if (cand.get("address") or {}).get("state") != "Washington":
        return 0
    want = set(tokens(place_name))
    have = set(tokens(cand.get("name") or ""))
    if not want or not have:
        return 0
    s = len(want & have) / len(want)
    if house and (cand.get("address") or {}).get("house_number") == house:
        s += 0.5
    if (cand.get("extratags") or {}).get("opening_hours"):
        s += 0.05
    return s


def apply(data):
    """Write confident matches (every word of our name found in the OSM name) into places.json."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from update import all_places, clean_url, format_address
    cands = json.loads(CANDIDATES.read_text())
    n = 0
    for p in all_places(data):
        c = cands.get(p["id"])
        if not c or c["score"] < 0.99 or p.get("osm_src") == "manual":
            continue
        tags = c.get("extratags") or {}
        p["osm"] = c["osm"]
        for field, value in (("ad", format_address(c.get("address"))),
                             ("web", clean_url(tags.get("website") or tags.get("contact:website") or tags.get("url"))),
                             ("hours", tags.get("opening_hours"))):
            if value and p.get(field + "_src") != "manual":
                p[field] = value
        n += 1
    PLACES.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"applied {n} matches")


def main():
    data = json.loads(PLACES.read_text())
    if "--apply" in sys.argv:
        return apply(data)
    out = json.loads(CANDIDATES.read_text()) if CANDIDATES.exists() else {}
    todo = [(c, p) for c in data["cities"] for sec in c["sections"].values() for p in sec]
    for i, (c, p) in enumerate(todo, 1):
        if p["id"] in out:
            continue
        name = re.sub(r"\s*\(.*?\)", "", p["n"]).strip()
        q = p.get("q")
        town = c["name"]
        house = None
        if q:
            parts = [x.strip() for x in q.split(",")]
            if len(parts) >= 3:
                town = parts[-2]
            m = re.search(r",\s*(\d+)\s", q)
            house = m.group(1) if m else None
        queries = [f"{name}, {town}, Washington", f"{name}, Washington"]
        best, best_s, tried = None, 0, []
        for query in queries:
            res = search(query)
            time.sleep(1.1)
            tried.append({"q": query, "names": [r.get("name") for r in res][:5]})
            for r in res:
                s = score(name, house, r)
                if s > best_s:
                    best, best_s = r, s
            if best_s >= 0.99:
                break
        rec = {"name": p["n"], "city": c["name"], "score": round(best_s, 2), "tried": tried}
        if best:
            rec["osm"] = best["osm_type"][0].upper() + str(best["osm_id"])
            rec["osm_name"] = best.get("name")
            rec["address"] = best.get("address")
            rec["extratags"] = best.get("extratags")
            rec["lat"], rec["lon"] = best.get("lat"), best.get("lon")
            rec["category"] = f'{best.get("category")}/{best.get("type")}'
        out[p["id"]] = rec
        print(f"{i}/{len(todo)} {best_s:.2f} {p['n']} -> {rec.get('osm_name')}", flush=True)
        if i % 20 == 0:
            CANDIDATES.write_text(json.dumps(out, indent=1))
    CANDIDATES.write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    sys.exit(main())
