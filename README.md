# Washington City Tour Guide

A pocket web app for rideshare drivers: where to eat, stay and visit in eight Washington cities (Seattle, Bellevue, Tacoma, Olympia, Leavenworth, Bellingham, Spokane and Vancouver), with prices, opening hours and one-tap directions in Google Maps, Waze or Apple Maps.

Live site: https://laminedo.github.io/citytourguide/

## How it is put together

- `data/places.json` is the list of places. Edit this to add, remove or correct a place.
- `src/template.html` is the app itself.
- `tools/update.py` refreshes addresses, websites and opening hours from OpenStreetMap and rebuilds `index.html`.
- `tools/match_osm.py` finds newly added places in OpenStreetMap.

## Updating

```bash
python3 tools/update.py
```

Use `python3 tools/update.py --build-only` to rebuild without going online.

Prices are typical ranges and are entered by hand. Addresses, websites and hours come from OpenStreetMap (© OpenStreetMap contributors, ODbL) and can lag behind the real world, so the app also links each place to its Google listing.
