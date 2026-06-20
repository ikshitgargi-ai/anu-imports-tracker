#!/usr/bin/env python3
"""Build the bundled RPR Tasting Blitz seed file (data/rpr_blitz_seed.json).

Merges two READ-ONLY sources by store number into one committed artifact so
production ingest (POST /api/rpr/ingest) never needs access to Downloads/ or
ASPL/ on this Mac:

  1. ASPL deploy list  (rpr-stores.json)
       {updated, cities: {<city>: [lat, lng]}, stores: [[store_no_str, city, address], ...]}
       148 stores. The third field is a real street address when it starts with
       a digit (96 stores); otherwise it is a cross-street string that
       duplicates the DOCX and is dropped (address stays '').
  2. RPR LCBO RETAIL LIST.docx (word/document.xml via stdlib zipfile + regex)
       Lines like "8 Brampton (Store 773) – Queen St E &amp; Airport Rd"
       -> cross_streets "Queen St E & Airport Rd" (XML entities unescaped).

Output: one record per store, json-sorted in source (city-alphabetical) order —
clustering downstream is deterministic given this order:
  {store_number:int, city, address, cross_streets, centroid_lat, centroid_lng}

Every record carries the city-centroid coords; stores with address '' rely on
them until the stores-table / Nominatim geocode pass upgrades lat/lng at ingest.

Run from anywhere:  python3 scripts/build_rpr_seed.py
Re-running is safe (deterministic, overwrites the artifact in place).
"""
import html
import json
import os
import re
import sys
import zipfile

# READ-ONLY source paths (never written to)
RPR_STORES_JSON = '/Users/ikshitsharma/Desktop/ASPL/deploy/rpr-stores.json'
RPR_RETAIL_DOCX = '/Users/ikshitsharma/Downloads/RPR LCBO RETAIL LIST.docx'

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, 'data', 'rpr_blitz_seed.json')

# "1 Ajax (Store 191) – Salem &amp; Taunton"  (en/em dash or hyphen after store no.)
DOCX_LINE = re.compile(r'^\s*\d+\s+(?P<city>.+?)\s+\(Store\s+(?P<num>\d+)\)\s*[–—-]\s*(?P<cross>.+?)\s*$')
W_PARA = re.compile(r'<w:p[ >].*?</w:p>', re.S)
W_TEXT = re.compile(r'<w:t[^>]*>(.*?)</w:t>', re.S)
STREET_ADDRESS = re.compile(r'^\d')  # real addresses start with a street number


def parse_docx_cross_streets(path):
    """store_number(int) -> cross_streets(str), from word/document.xml."""
    with zipfile.ZipFile(path) as z:
        xml = z.read('word/document.xml').decode('utf-8')
    out = {}
    for para in W_PARA.findall(xml):
        text = ''.join(W_TEXT.findall(para))
        m = DOCX_LINE.match(html.unescape(text))
        if m:
            out[int(m.group('num'))] = m.group('cross').strip()
    return out


def build_records():
    with open(RPR_STORES_JSON) as f:
        src = json.load(f)
    cities = src['cities']            # {<city>: [lat, lng]}
    cross_by_num = parse_docx_cross_streets(RPR_RETAIL_DOCX)

    records = []
    for store_no_str, city, raw_addr in src['stores']:
        store_number = int(store_no_str)
        raw_addr = (raw_addr or '').strip()
        # Cross-street strings ("Bayfield & Hanmer") masquerade as addresses in
        # the json — only keep values that start with a street number.
        address = raw_addr if STREET_ADDRESS.match(raw_addr) else ''
        cross_streets = cross_by_num.get(store_number, '')
        if not cross_streets and address == '' and raw_addr:
            cross_streets = raw_addr  # fallback: json cross-street if DOCX missed it
        centroid = cities.get(city) or [0, 0]
        records.append({
            'store_number': store_number,
            'city': city,
            'address': address,
            'cross_streets': cross_streets,
            'centroid_lat': round(float(centroid[0]), 6),
            'centroid_lng': round(float(centroid[1]), 6),
        })
    return records


def main():
    records = build_records()

    # Sanity gates — fail loudly rather than commit a short artifact.
    nums = [r['store_number'] for r in records]
    if len(nums) != len(set(nums)):
        sys.exit('FATAL: duplicate store numbers in merged records')
    if len(records) < 140:
        sys.exit('FATAL: expected ~148 records, got %d — check sources' % len(records))
    missing_centroid = [r['store_number'] for r in records
                        if not r['centroid_lat'] and not r['centroid_lng']]
    if missing_centroid:
        sys.exit('FATAL: stores with no city centroid: %s' % missing_centroid)
    no_cross = [r['store_number'] for r in records if not r['cross_streets']]

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w') as f:
        json.dump(records, f, indent=1, ensure_ascii=False)
        f.write('\n')

    with_addr = sum(1 for r in records if r['address'])
    print('wrote %s' % OUT_PATH)
    print('records: %d | street addresses: %d | centroid-only: %d | missing cross_streets: %d'
          % (len(records), with_addr, len(records) - with_addr, len(no_cross)))


if __name__ == '__main__':
    main()
