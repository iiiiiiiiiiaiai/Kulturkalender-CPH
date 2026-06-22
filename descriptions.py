import requests, re, time
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "da-DK,da;q=0.9",
}

SKIP_URLS = {
    "https://mogensdahl.dk/koncerter",
    "https://www.drkoncerthuset.dk/kalender/",
    "https://www.kglteater.dk/kalender/",
    "https://www.tivoli.dk/program/sommerklassisk",
    "https://www.tivoli.dk/program/jazz-i-tivoli",
}

def fetch_meta_description(url):
    if not url or url in ("#", "") or url in SKIP_URLS:
        return ""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "lxml")
        for attr in [{"name":"description"}, {"property":"og:description"}]:
            tag = soup.find("meta", attrs=attr)
            if tag and tag.get("content","").strip():
                return re.sub(r'\s+',' ', tag["content"]).strip()[:150]
        for p in soup.find_all("p"):
            text = re.sub(r'\s+',' ', p.get_text()).strip()
            if 40 < len(text) < 400:
                return text[:150] + ("…" if len(text) > 150 else "")
    except Exception:
        pass
    return ""

def add_descriptions(events):
    no_desc = [e for e in events if not e.get("description","").strip()
               and e.get("link","") not in ("","#") and e.get("link","") not in SKIP_URLS]
    if not no_desc:
        return events
    print(f"\n📝 Henter beskrivelser fra {len(no_desc)} sider...")
    for i, ev in enumerate(no_desc, 1):
        ev["description"] = fetch_meta_description(ev["link"])
        if i % 5 == 0 or i == len(no_desc):
            found = sum(1 for e in no_desc[:i] if e.get("description","").strip())
            print(f"  → {i}/{len(no_desc)} tjekket · {found} fundet")
        time.sleep(0.3)
    return events
