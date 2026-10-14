#!/usr/bin/env python3
"""Download Wikimedia Commons images for occupation categories.

Resolves occupation slugs to Wikidata QIDs, fetches their P18 (image)
property via SPARQL, then downloads and optimizes the thumbnails.

Run inside the backend container:
    docker compose exec backend python /app/scripts/download_occupation_images.py
"""

import os
import sys
import time
import urllib.parse
import urllib.request
import json
from io import BytesIO
from pathlib import Path

from PIL import Image

IMAGE_DIR = Path(os.environ.get("IMAGE_CACHE_DIR", "/app/images"))
MAX_DIM = 300
JPEG_QUALITY = 82
USER_AGENT = "HumansMap/1.0 (admin@humansmap.com)"
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# Occupation slug → Wikidata QID. Slugs come from the DB's primary_occupation field;
# QIDs are stable Wikidata identifiers for each profession. This mapping is needed
# because our slugs are normalized English labels, not QIDs.
SLUG_TO_QID = {
    "politician": "Q82955",
    "actor": "Q33999",
    "writer": "Q36180",
    "university_teacher": "Q1622272",
    "football_player": "Q937857",
    "painter": "Q1028181",
    "historian": "Q201788",
    "journalist": "Q1930187",
    "mathematician": "Q170790",
    "composer": "Q36834",
    "singer": "Q177220",
    "basketball_player": "Q3665646",
    "poet": "Q49757",
    "film_director": "Q2526255",
    "economist": "Q188094",
    "musician": "Q639669",
    "architect": "Q42973",
    "military_person": "Q47064",
    "lawyer": "Q40348",
    "physicist": "Q169470",
    "philosopher": "Q4964182",
    "screenwriter": "Q28389",
    "photographer": "Q33231",
    "sculptor": "Q1281618",
    "military_officer": "Q189290",
    "engineer": "Q81096",
    "botanist": "Q2374149",
    "diplomat": "Q193391",
    "chemist": "Q593644",
    "businessperson": "Q43845",
    "teacher": "Q37226",
    "baseball_player": "Q10871364",
    "sociologist": "Q2306091",
    "film_actor": "Q10800557",
    "conductor": "Q158852",
    "political_scientist": "Q1238570",
    "pianist": "Q486748",
    "entrepreneur": "Q131524",
    "scientist": "Q901",
    "biologist": "Q864503",
    "film_producer": "Q3282637",
    "judge": "Q16533",
    "publisher": "Q2516866",
    "badminton_player": "Q13381376",
    "zoologist": "Q350979",
    "record_producer": "Q183945",
    "television_actor": "Q10843263",
    "tennis_player": "Q10833314",
    "american_football_player": "Q19204627",
    "television_presenter": "Q947873",
    "model": "Q4610556",
    "guitarist": "Q855091",
    "psychiatrist": "Q211236",
    "banker": "Q806798",
    "printmaker": "Q11569986",
    "public_official": "Q2285706",
    "voice_actor": "Q2405480",
    "civil_servant": "Q212071",
    "theatre_director": "Q3387717",
    "swimmer": "Q10843402",
    "philanthropist": "Q11631206",
    "essayist": "Q11774202",
    "volleyball_player": "Q15117302",
    "sprinter": "Q4009406",
    "alpine_skier": "Q4144610",
    "boxer": "Q11338576",
    "trade_unionist": "Q15627169",
    "fencer": "Q13382576",
    "fashion_designer": "Q3501317",
    "activist": "Q15253558",
    "clergyman": "Q2259532",
    "astronaut": "Q11631",
    "podcaster": "Q15077007",
    "financier": "Q1397016",
    "water_polo_player": "Q13474373",
    "consultant": "Q860918",
    "executive": "Q484876",
    "football_manager": "Q628099",
    "field_hockey_player": "Q10843402",
    "criminal": "Q2159907",
    "keyboardist": "Q12800682",
    "investor": "Q1233720",
    "lobbyist": "Q1623997",
    "intelligence_officer": "Q15303838",
    "board_member": "Q726711",
    "cyclist": "Q2309784",
    "rugby_player": "Q14089670",
}

# For occupations whose Wikidata item lacks P18, fall back to a related item's image
FALLBACK_QID = {
    "film_actor": "Q33999",        # → actor
    "television_actor": "Q33999",  # → actor
}


def sparql_query(query):
    url = SPARQL_ENDPOINT + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["results"]["bindings"]


def fetch_image_urls(qids):
    """Fetch P18 image URLs for a list of Wikidata QIDs via SPARQL."""
    result = {}
    # Process in chunks to stay under SPARQL query limits
    for i in range(0, len(qids), 50):
        chunk = qids[i:i + 50]
        values = " ".join(f"wd:{q}" for q in chunk)
        query = f"""
        SELECT ?item ?img WHERE {{
          VALUES ?item {{ {values} }}
          ?item wdt:P18 ?img .
        }}
        """
        for row in sparql_query(query):
            qid = row["item"]["value"].split("/")[-1]
            if qid not in result:
                result[qid] = row["img"]["value"]
        if i + 50 < len(qids):
            time.sleep(1)
    return result


def image_url_to_thumb_url(image_url, width=300):
    if "Special:FilePath/" in image_url:
        filename = image_url.split("Special:FilePath/")[-1]
    else:
        filename = image_url.rsplit("/", 1)[-1]
    return f"https://commons.wikimedia.org/w/thumb.php?f={filename}&w={width}"


def download_and_save(slug, image_url):
    output_path = IMAGE_DIR / f"{slug}.jpg"
    if output_path.exists():
        print(f"  SKIP {slug} (already exists)")
        return True

    thumb_url = image_url_to_thumb_url(image_url, width=MAX_DIM)
    try:
        req = urllib.request.Request(thumb_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw_bytes = resp.read()
    except Exception as e:
        print(f"  FAIL {slug}: download error: {e}")
        return False

    try:
        img = Image.open(BytesIO(raw_bytes))
        img = img.convert("RGB")
        img.save(str(output_path), "JPEG", quality=JPEG_QUALITY)
        size_kb = output_path.stat().st_size / 1024
        print(f"  OK   {slug} ({size_kb:.1f} KB)")
        return True
    except Exception as e:
        print(f"  FAIL {slug}: optimize error: {e}")
        if output_path.exists():
            os.remove(output_path)
        return False


def main():
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    # Collect all unique QIDs (including fallbacks)
    all_qids = list(set(SLUG_TO_QID.values()) | set(FALLBACK_QID.values()))
    print(f"Fetching image URLs for {len(all_qids)} Wikidata items...")
    qid_to_img = fetch_image_urls(all_qids)
    print(f"  Found images for {len(qid_to_img)} items\n")

    ok = 0
    fail = 0
    skip_no_img = 0

    for slug, qid in SLUG_TO_QID.items():
        img_url = qid_to_img.get(qid)
        # Try fallback if no image on primary QID
        if not img_url and slug in FALLBACK_QID:
            img_url = qid_to_img.get(FALLBACK_QID[slug])
        if not img_url:
            print(f"  SKIP {slug} (no Wikidata image)")
            skip_no_img += 1
            continue
        if download_and_save(slug, img_url):
            ok += 1
        else:
            fail += 1

    print(f"\nDone: {ok} downloaded, {skip_no_img} no image available, {fail} failed")


if __name__ == "__main__":
    main()
