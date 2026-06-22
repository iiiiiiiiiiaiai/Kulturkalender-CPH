"""
KØBENHAVNS KULTURKALENDER  v6
==============================
• Kalendervisning: dag-for-dag for koncerter, museumsoversigt for udstillinger
• SMK: cookie-banner afvises før scraping
• CC: kun nutid + kommende (ikke hele historikken)
• KglTeater: filtrerer dato-overskrifter fra, finder forestillingsnavne
• Alle sources: beskrivelse inkluderet

Kør: python3 main.py
"""

import asyncio, requests, re, smtplib, time
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict

from config import GMAIL_USER, GMAIL_PASSWORD, RECIPIENT

# ══════════════════════════════════════════════════════════════════════════════
#  KONSTANTER
# ══════════════════════════════════════════════════════════════════════════════

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "da-DK,da;q=0.9",
}

DANISH_MONTHS = {
    "januar":1,"februar":2,"marts":3,"april":4,"maj":5,"juni":6,
    "juli":7,"august":8,"september":9,"oktober":10,"november":11,"december":12,
}
DANISH_DAYS = ["mandag","tirsdag","onsdag","torsdag","fredag","lørdag","søndag"]
DANISH_MONTHS_SHORT = {
    1:"jan",2:"feb",3:"mar",4:"apr",5:"maj",6:"jun",
    7:"jul",8:"aug",9:"sep",10:"okt",11:"nov",12:"dec"
}

NON_CPH = [
    "maribo","odense","aarhus","aalborg","esbjerg","roskilde","helsingør",
    "hillerød","ringsted","næstved","slagelse","kolding","vejle","silkeborg",
    "herning","viborg","fredericia","holbæk","sønderborg","lolland","falster",
    "bornholm","malmö","malmø","stockholm","göteborg","lund",
]

# Ord der afslører cookie-tekst, navigation, eller ren støj
COOKIE_NAV_WORDS = {
    "nødvendige","funktionelle","statistiske","marketing","uklassificerede",
    "cookiepolitik","hvad er en cookie","hjemmesidens brug af cookies",
    "du bestemmer over dine data","ændring af dit samtykke",
    "sådan afviser eller sletter du cookies","hvordan sletter man cookies",
    "hvor længe bliver cookies gemt","har du spørgsmål",
    "åbningstider","entré","besøg smk","årskort","se også",
    "aktuelle udstillinger","kommende udstillinger","tidligere udstillinger",
    "fast udstilling med værker fra smk's samling",
    "med kunstsamlingen i centrum","vi forbereder nye udstillinger",
    "udvalgte udstillinger fra de seneste år","besøg smk",
    "åbningstider, priser, praktisk info m.m.","et helt års kunstoplevelser",
    "læs mere","se mere","read more",
}


# ══════════════════════════════════════════════════════════════════════════════
#  HJÆLPEFUNKTIONER
# ══════════════════════════════════════════════════════════════════════════════

def fetch(url, timeout=15):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status(); r.encoding = "utf-8"
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        print(f"  ⚠️  {url}: {e}"); return None

def clean(t):
    return re.sub(r'\s+', ' ', t or "").strip()

def parse_date(text):
    if not text: return None
    t = text.lower().strip()
    for pattern, handler in [
        (r'(\d{1,2})\.?\s+(' + '|'.join(DANISH_MONTHS) + r')\s+(\d{4})',
         lambda m: datetime(int(m.group(3)), DANISH_MONTHS[m.group(2)], int(m.group(1)))),
        (r'(\d{4})-(\d{2})-(\d{2})',
         lambda m: datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))),
        (r'(\d{1,2})[./](\d{1,2})[./](\d{2,4})',
         lambda m: datetime(int(m.group(3)) if int(m.group(3)) > 100
                            else int(m.group(3))+2000, int(m.group(2)), int(m.group(1)))),
    ]:
        m = re.search(pattern, t)
        if m:
            try: return handler(m)
            except: pass
    return None

def extract_time(text):
    m = re.search(r'kl\.?\s*(\d{1,2})[.:](\d{2})', (text or "").lower())
    if m: return f"kl. {m.group(1)}:{m.group(2)}"
    m = re.search(r'(\d{1,2}):(\d{2})', text or "")
    if m and int(m.group(1)) < 24: return f"kl. {m.group(1)}:{m.group(2)}"
    return ""

def within_days(date_str, days=8):
    dt = parse_date(date_str)
    if dt is None: return False
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return today <= dt <= today + timedelta(days=days)

def is_cph(event):
    text = " ".join([event.get("venue",""), event.get("title","")]).lower()
    return not any(city in text for city in NON_CPH)

def is_date_header(title):
    """True hvis titlen er en dato-overskrift, ikke et eventnavn."""
    return bool(re.match(
        r'^(mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)',
        title.lower()
    ))

def is_garbage(title):
    """True hvis titlen er cookie-tekst, navigation eller støj."""
    t = title.lower().strip()
    return (t in COOKIE_NAV_WORDS or len(t) < 5 or
            len(title) > 120 or  # Suspiciously long = probably a text blob
            is_date_header(title))

def dedup(events):
    seen, out = set(), []
    for e in events:
        k = e.get("title","").lower()[:80]
        if k and k not in seen: seen.add(k); out.append(e)
    return out

def make_event(title, date, venue, link, source, category, description="", time_str=""):
    return {"title":title, "date":date, "venue":venue, "link":link,
            "source":source, "category":category,
            "description":description, "time":time_str}


# ══════════════════════════════════════════════════════════════════════════════
#  PLAYWRIGHT – HJÆLPER
# ══════════════════════════════════════════════════════════════════════════════

async def dismiss_cookies(page):
    """Forsøger at afvise cookie-banner (CookieBot og andre)."""
    for sel in [
        '#CybotCookiebotDialogBodyButtonAccept',
        'button:has-text("Tillad alle")',
        'button:has-text("Accepter alle")',
        'button:has-text("Accept all")',
        'button:has-text("Acceptér alle")',
        '[id*="accept-all"]',
        '[class*="accept-all"]',
    ]:
        try:
            await page.click(sel, timeout=3000)
            await page.wait_for_timeout(1500)
            return
        except: pass

async def pw_get(browser, url, extra_wait=3000, dismiss_cookie=False):
    """Henter URL med Playwright, returnerer BeautifulSoup."""
    page = await browser.new_page()
    soup = None
    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if dismiss_cookie:
            await page.wait_for_timeout(2000)
            await dismiss_cookies(page)
        await page.wait_for_timeout(extra_wait)
        html  = await page.content()
        soup  = BeautifulSoup(html, "lxml")
    except Exception as e:
        print(f"  ⚠️  Playwright ({url[:50]}): {e}")
    finally:
        await page.close()
    return soup


# ══════════════════════════════════════════════════════════════════════════════
#  PLAYWRIGHT SCRAPERS – KONCERTER
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_dr(browser, ensemble_id, source):
    print(f"  → {source}...")
    cat  = "🎻 Klassisk" if ensemble_id == "1210" else "🎷 Jazz"
    url  = f"https://www.drkoncerthuset.dk/kalender/?Ensemble={ensemble_id}"
    soup = await pw_get(browser, url, extra_wait=4000)
    if not soup: return []

    events = []
    # DR bruger kort-struktur efter JS-rendering
    for card in soup.select("article, [class*='event'], [class*='card'], [class*='program']"):
        title_el = card.select_one("h2,h3,h4,[class*='title'],[class*='name']")
        date_el  = card.select_one("time,[class*='date'],[class*='dato']")
        if not title_el: continue
        title    = clean(title_el.get_text())
        if is_garbage(title): continue
        date_str = (date_el.get("datetime","") or clean(date_el.get_text())) if date_el else ""
        time_str = extract_time(card.get_text())
        link_el  = card.find("a", href=True)
        href     = link_el["href"] if link_el else url
        link     = href if href.startswith("http") else "https://www.drkoncerthuset.dk"+href
        ev = make_event(title, date_str, "DR Koncerthuset", link, source, cat, time_str=time_str)
        if is_cph(ev) and within_days(date_str): events.append(ev)

    result = dedup(events)
    print(f"     ✓ {len(result)} events")
    return result


async def scrape_tivoli(browser, path, source, cat):
    print(f"  → {source}...")
    url  = f"https://www.tivoli.dk{path}"
    soup = await pw_get(browser, url, extra_wait=5000)
    if not soup: return []

    events = []
    for card in soup.select("article,[class*='event'],[class*='concert'],[class*='program'],[class*='card']"):
        title_el = card.select_one("h2,h3,h4,[class*='title'],[class*='name']")
        date_el  = card.select_one("time,[class*='date'],[class*='dato']")
        if not title_el: continue
        title    = clean(title_el.get_text())
        if is_garbage(title): continue
        date_str = (date_el.get("datetime","") or clean(date_el.get_text())) if date_el else ""
        time_str = extract_time(card.get_text())
        link_el  = card.find("a", href=True)
        href     = link_el["href"] if link_el else url
        link     = href if href.startswith("http") else "https://www.tivoli.dk"+href
        ev = make_event(title, date_str, "Tivoli", link, source, cat, time_str=time_str)
        if within_days(date_str): events.append(ev)

    result = dedup(events)
    print(f"     ✓ {len(result)} events")
    return result


async def scrape_kgl_teater(browser):
    print("  → Det Kongelige Teater...")
    url  = "https://www.kglteater.dk/kalender/"
    soup = await pw_get(browser, url, extra_wait=5000)
    if not soup: return []

    events = []
    # KglTeater viser kalender med dato-overskrifter og forestillinger under
    # Strategi: find dato-celler og udpak forestillingens navn (h4/h5 eller dybere)
    for day_el in soup.find_all(["h2","h3"], string=re.compile(
            r'(mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)', re.I)):
        date_str = clean(day_el.get_text())
        container = day_el.find_parent(["div","section","li","article"])
        if not container: continue

        # Find forestillingsnavne inden for denne dags container
        for perf_el in container.select("h4,h5,[class*='title'],[class*='show'],[class*='performance']"):
            title = clean(perf_el.get_text())
            if not title or is_garbage(title): continue
            if title == date_str: continue
            time_str = extract_time(container.get_text())
            link_el  = perf_el.find("a", href=True) or container.find("a", href=True)
            href     = link_el["href"] if link_el else url
            link     = href if href.startswith("http") else "https://www.kglteater.dk"+href
            desc_el  = container.select_one("p,[class*='desc'],[class*='summary']")
            desc     = clean(desc_el.get_text())[:120] if desc_el else ""
            ev = make_event(title, date_str, "Det Kongelige Teater / Operaen",
                            link, "Det Kongelige Teater", "🎻 Klassisk", desc, time_str)
            if within_days(date_str): events.append(ev)

    # Fallback: hvis ovenstående ikke finder noget, prøv generic + filtrer datooverskrifter
    if not events:
        for card in soup.select("article,[class*='event'],[class*='card']"):
            title_el = card.select_one("h2,h3,h4,[class*='title']")
            date_el  = card.select_one("time,[class*='date']")
            if not title_el: continue
            title    = clean(title_el.get_text())
            if is_garbage(title): continue
            date_str = (date_el.get("datetime","") or clean(date_el.get_text())) if date_el else ""
            time_str = extract_time(card.get_text())
            link_el  = card.find("a", href=True)
            href     = link_el["href"] if link_el else url
            link     = href if href.startswith("http") else "https://www.kglteater.dk"+href
            ev = make_event(title, date_str, "Det Kongelige Teater / Operaen",
                            link, "Det Kongelige Teater", "🎻 Klassisk", time_str=time_str)
            if within_days(date_str): events.append(ev)

    result = dedup(events)
    print(f"     ✓ {len(result)} events")
    return result


async def scrape_church(browser, url, name):
    print(f"  → {name}...")
    soup = await pw_get(browser, url, extra_wait=5000)
    if not soup: return []

    SKIP = {"gudstjeneste","konfirmation","dåb","vielse","begravelse",
            "sognebåndsløsning","sjælesorg","menighedsråd","kirkeskat",
            "organist","kantor","discografi","klokkespil","orgler",
            "flytte","arkiv","pressemeddelels","nyhedsbrev","kontakt",
            "åbningstider","om kirken","besøg"}

    events = []
    for card in soup.select("[class*='event'],[class*='arrangement'],[class*='churchdesk'],article,li"):
        title_el = card.select_one("h2,h3,h4,[class*='title'],[class*='name'],a")
        date_el  = card.select_one("time,[class*='date'],[class*='dato']")
        if not title_el: continue
        title    = clean(title_el.get_text())
        if not title or is_garbage(title): continue
        if any(s in title.lower() for s in SKIP): continue
        date_str = (date_el.get("datetime","") or clean(date_el.get_text())) if date_el else ""
        time_str = extract_time(card.get_text())
        link_el  = card.find("a", href=True)
        href     = link_el["href"] if link_el else url
        link     = href if href.startswith("http") else url
        ev = make_event(title, date_str, name, link, name, "🎻 Klassisk", time_str=time_str)
        if within_days(date_str): events.append(ev)

    result = dedup(events)
    print(f"     ✓ {len(result)} events")
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  PLAYWRIGHT SCRAPERS – UDSTILLINGER
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_smk(browser):
    print("  → SMK...")
    url  = "https://www.smk.dk/article/udstillinger/"
    soup = await pw_get(browser, url, extra_wait=5000, dismiss_cookie=True)
    if not soup: return []

    events = []
    # SMK udstillingskort: find specifikke udstillingssider
    for a in soup.find_all("a", href=True):
        href  = a["href"]
        title = clean(a.get_text())
        if not title or is_garbage(title): continue
        if len(title) > 80: continue

        # SMK udstillingslinks har typisk /udstilling/ eller /exhibition/ i URL
        if not any(kw in href for kw in ["/udstilling/","/exhibition/","/article/"]): continue
        if href.rstrip("/").endswith(("/udstillinger","/exhibitions","/article/udstillinger")): continue

        # Find dato i nærheden
        parent   = a.find_parent(["div","article","li","section"]) or a.parent
        date_str = ""
        desc_str = ""
        if parent:
            for txt in parent.find_all(string=True):
                t = txt.strip()
                if parse_date(t) and not date_str:
                    date_str = t[:60]
            desc_el = parent.select_one("p,[class*='desc'],[class*='intro']")
            if desc_el: desc_str = clean(desc_el.get_text())[:120]

        link = href if href.startswith("http") else "https://www.smk.dk"+href
        events.append(make_event(title, date_str or "Løbende",
                                 "SMK – Statens Museum for Kunst",
                                 link, "SMK", "🖼️ Udstilling", desc_str))

    result = dedup(events)
    print(f"     ✓ {len(result)} udstillinger")
    return result


async def scrape_glyptoteket(browser):
    print("  → Glyptoteket...")
    url  = "https://glyptoteket.dk/udstillinger/aktuelle-udstillinger"
    soup = await pw_get(browser, url, extra_wait=4000)
    if not soup: return []

    NAV = {"udstillinger","aktuelle udstillinger","kommende udstillinger",
           "permanente udstillinger","tidligere udstillinger","besøg","billetter",
           "café","kalender","omvisninger","om museet","podcast","forskning",
           "sommerkoncerter","torsdag på taget","fremtidens glyptotek"}

    events = []
    for card in soup.select("article,[class*='exhibition'],[class*='udstilling'],[class*='card']"):
        title_el = card.select_one("h2,h3,h4,[class*='title'],[class*='name']")
        date_el  = card.select_one("time,[class*='date'],[class*='period']")
        if not title_el: continue
        title    = clean(title_el.get_text())
        if title.lower() in NAV or is_garbage(title): continue
        date_str = (date_el.get("datetime","") or clean(date_el.get_text())) if date_el else "Løbende"
        desc_el  = card.select_one("p,[class*='desc']")
        desc     = clean(desc_el.get_text())[:120] if desc_el else ""
        link_el  = card.find("a", href=True)
        href     = link_el["href"] if link_el else url
        link     = href if href.startswith("http") else "https://glyptoteket.dk"+href
        events.append(make_event(title, date_str, "Ny Carlsberg Glyptotek",
                                 link, "Glyptoteket", "🖼️ Udstilling", desc))

    # Fallback: link-baseret
    if not events:
        for a in soup.find_all("a", href=True):
            href  = a["href"]
            title = clean(a.get_text())
            if not title or title.lower() in NAV or is_garbage(title): continue
            if "/udstillinger/" not in href: continue
            link = href if href.startswith("http") else "https://glyptoteket.dk"+href
            events.append(make_event(title, "Løbende", "Ny Carlsberg Glyptotek",
                                     link, "Glyptoteket", "🖼️ Udstilling"))

    result = dedup(events)
    print(f"     ✓ {len(result)} udstillinger")
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  STATISKE SCRAPERS
# ══════════════════════════════════════════════════════════════════════════════

def scrape_copenhagenphil():
    print("  → Copenhagen Phil...")
    soup = fetch("https://copenhagenphil.dk/koncerter/kategori/klassisk/")
    if not soup: return []
    SKIP = ["/kategori/","/afholdte-","/billetter","/saesonprogram",
            "/orkestret","/skoler","/udforsk","/om-os"]
    events = []
    for h2 in soup.find_all("h2"):
        a = h2.find("a", href=True)
        if not a: continue
        href = a["href"]
        if not href or any(s in href for s in SKIP) or "/koncerter/" not in href: continue
        title = clean(a.get_text())
        if not title or is_garbage(title): continue
        parent   = h2.find_parent("li") or h2.find_parent("article") or h2.parent
        date_str = ""
        if parent:
            for txt in parent.find_all(string=True):
                t = txt.strip()
                if any(m in t.lower() for m in DANISH_MONTHS) and any(c.isdigit() for c in t):
                    date_str = t[:60]; break
        time_str = extract_time(parent.get_text() if parent else "")
        venue    = "Copenhagen Phil"
        if parent:
            vt = parent.find(class_=lambda c: c and "venue" in str(c).lower())
            if vt: venue = clean(vt.get_text())
        ev = make_event(title, date_str, venue, href,
                        "Copenhagen Phil", "🎻 Klassisk", time_str=time_str)
        if is_cph(ev) and within_days(date_str): events.append(ev)
    result = dedup(events)
    print(f"     ✓ {len(result)} events")
    return result


def scrape_mogensdahl():
    print("  → Mogens Dahl Koncertsal...")
    soup = fetch("https://mogensdahl.dk/koncerter")
    if not soup: return []
    SKIP = ["konferencer","dagsmøder","kick-off","firmafest","bryllup","nytårskur",
            "julefest","privatfest","kammerkor","om os","kontakt","gave","gaveide",
            "billetter","nyhedsbrev","presse","stilling","møder","vores mad"]
    events = []
    for h3 in soup.find_all("h3"):
        title = clean(h3.get_text())
        if not title or is_garbage(title) or any(s in title.lower() for s in SKIP): continue
        parent = h3.find_parent(["section","div","article","li"]) or h3.parent
        date_str = ""
        if parent:
            for txt in parent.find_all(string=True):
                t = txt.strip()
                if any(m in t.lower() for m in DANISH_MONTHS) and any(c.isdigit() for c in t):
                    date_str = t[:60]; break
        time_str = extract_time(parent.get_text() if parent else "")
        link     = "https://mogensdahl.dk/koncerter"
        if parent:
            a = parent.find("a", href=True)
            if a:
                h = a["href"]
                link = h if h.startswith("http") else "https://mogensdahl.dk"+h
        desc_el = parent.select_one("p") if parent else None
        desc    = clean(desc_el.get_text())[:120] if desc_el else ""
        ev = make_event(title, date_str, "Mogens Dahl Koncertsal",
                        link, "Mogens Dahl", "🎻 Klassisk", desc, time_str)
        if within_days(date_str): events.append(ev)
    result = dedup(events)
    print(f"     ✓ {len(result)} events")
    return result


def scrape_hirschsprung():
    print("  → Hirschsprung...")
    soup = fetch("https://hirschsprung.dk/udstillinger")
    if not soup: return []
    events = []
    in_section = False
    for el in soup.find_all(["h2","h3","li"]):
        text = clean(el.get_text())
        if el.name in ["h2","h3"]:
            in_section = text.lower() in ["aktuelle","kommende"]
            continue
        if not in_section: continue
        a = el.find("a", href=True)
        if not a: continue
        raw  = clean(a.get_text())
        href = a["href"]
        link = href if href.startswith("http") else "https://hirschsprung.dk"+href
        dm = re.search(r'\d{1,2}\.\s+\w+\s+\d{4}', raw)
        title    = raw[:dm.start()].strip(" –-") if dm else raw
        date_str = raw[dm.start():].strip() if dm else "Løbende"
        if title and len(title) > 4:
            events.append(make_event(title, date_str,
                                     "Den Hirschsprungske Samling",
                                     link, "Hirschsprung", "🖼️ Udstilling"))
    result = dedup(events)
    print(f"     ✓ {len(result)} udstillinger")
    return result


def scrape_copenhagencontemporary():
    """CC: KUN nutid + kommende, ikke historikken."""
    print("  → Copenhagen Contemporary...")
    soup = fetch("https://copenhagencontemporary.org/udstillinger/")
    if not soup: return []

    events = []
    # Find sektionerne "Nutid" og "Kommende" – stop ved "Fortid"
    collecting = False
    for el in soup.find_all(["h3","div","article","figure","a"]):
        # Tjek sektionsskift
        if el.name == "h3":
            section = clean(el.get_text()).lower()
            collecting = section in ["nutid","kommende"]
            if section == "fortid": break
            continue
        if not collecting: continue

        # Vi leder efter et <a> der peger på en specifik udstillingsside
        if el.name != "a": continue
        href = el.get("href","")
        if not href or any(nav in href for nav in [
            "/besog/","/events/","/udstillinger/","/om/","/presse/",
            "/familier/","/tilgaengelighed/","/cafe/","/medlemskab/",
            "/haandvaerker-","/guidede-","/CC-Skills","mailto:","#"]): continue

        # h4-elementer i forælderelementet udgør [Udstilling]-label, dato og titel
        parent = el.find_parent(["div","article","li","figure"])
        if not parent: continue
        h4s = [clean(h.get_text()) for h in parent.find_all("h4")]
        h4s = [h for h in h4s if h.lower() not in ("udstilling","")]
        if not h4s: continue

        date_str, title_parts = "", []
        for h in h4s:
            if (re.search(r'\d{2}\.\d{2}\.\d{2,4}', h) or
                    any(m in h.lower() for m in DANISH_MONTHS) or
                    any(w in h.lower() for w in ["åbning","permanent","chance","sidste"])):
                if not date_str: date_str = h
            else:
                title_parts.append(h)

        title = " ".join(title_parts).strip()
        if not title or len(title) < 4: continue

        link = href if href.startswith("http") else "https://copenhagencontemporary.org"+href
        events.append(make_event(title, date_str or "Løbende",
                                 "Copenhagen Contemporary",
                                 link, "CC", "🖼️ Udstilling"))

    result = dedup(events)
    print(f"     ✓ {len(result)} udstillinger")
    return result


def scrape_designmuseum():
    print("  → Designmuseum Danmark...")
    soup = fetch("https://designmuseum.dk/udstilling/")
    if not soup: return []
    NAV = {"nuværende","kommende","arkiv","da","en","kontakt","presse","støt","læs mere"}
    events = []
    for a in soup.find_all("a", href=True):
        href  = a["href"]
        title = clean(a.get_text())
        if not title or title.lower() in NAV or is_garbage(title): continue
        if "/udstilling/" not in href: continue
        if href.rstrip("/").endswith("/udstilling") or "?future" in href: continue
        if any(x in href for x in ["/arkiv/","mailto:","#"]): continue

        parent   = a.find_parent(["div","section","article","li"]) or a.parent
        date_str = ""
        if parent:
            full = parent.get_text(" ")
            m    = re.search(r'\d{1,2}\.?\s+\w+\s+[–—-]\s*\d{1,2}\.?\s+\w+\s+\d{4}', full)
            if m: date_str = m.group(0)
        # Find beskrivelse
        desc_el = parent.select_one("p") if parent else None
        desc    = clean(desc_el.get_text())[:120] if desc_el else ""
        link    = href if href.startswith("http") else "https://designmuseum.dk"+href
        events.append(make_event(title, date_str or "Løbende",
                                 "Designmuseum Danmark",
                                 link, "Designmuseum", "🖼️ Udstilling", desc))
    result = dedup(events)
    print(f"     ✓ {len(result)} udstillinger")
    return result


def scrape_thorvaldsen():
    print("  → Thorvaldsens Museum...")
    soup = fetch("https://www.thorvaldsensmuseum.dk/udstillinger")
    if not soup: return []
    NAV = {"samlingerne","nyhedsbrev","presse","forskning","undervisning",
           "om museet","besøg museet","opdag thorvaldsen","privatlivspolitik",
           "sitemap","kommende udstillinger","tidligere udstillinger",
           "udstillinger","køb billet","dit og mit thorvaldsen"}
    events = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/udstillinger/" not in href: continue
        if any(x in href for x in ["kommende-","tidligere-","#"]): continue
        raw   = clean(a.get_text())
        title = re.sub(r'(SÆRUDSTILLING|PERMANENT UDSTILLING)\s*','',raw,flags=re.I).strip()
        ws = title.split(); h = len(ws)//2
        if h > 2 and ws[:h] == ws[h:]: title = ' '.join(ws[:h])
        if not title or title.lower() in NAV or is_garbage(title): continue
        link = href if href.startswith("http") else "https://www.thorvaldsensmuseum.dk"+href
        events.append(make_event(title, "Løbende", "Thorvaldsens Museum",
                                 link, "Thorvaldsen", "🖼️ Udstilling"))
    result = dedup(events)
    print(f"     ✓ {len(result)} udstillinger")
    return result


def scrape_arken():
    print("  → ARKEN...")
    soup = fetch("https://www.arken.dk/udstillinger")
    if not soup: return []
    NAV = {"udstillinger","for børn","kalender","café","arkitektur & natur","samlingen",
           "fra sofaen","billetter","omvisninger","medlemskab","shop","information",
           "community","undervisning","om museet","forskning","presse","da","en",
           "nutid","fremtid","fortid","+ se mere","partner netværk"}
    events = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/udstillinger/" not in href: continue
        raw = clean(a.get_text())
        if not raw or raw.lower() in NAV or is_garbage(raw): continue
        dm = re.search(r'(\d{2}\.\d{2}\.\d{2,4})', raw)
        if dm:
            date_str = raw[dm.start():].replace("**","").strip()
            title    = re.sub(r'([a-zæøå])([A-ZÆØÅ]{2,})',r'\1 \2',raw[:dm.start()]).strip()
        else:
            date_str = "Løbende"
            title    = re.sub(r'([a-zæøå])([A-ZÆØÅ]{2,})',r'\1 \2',raw).strip()
        if not title or len(title) < 4: continue
        link = href if href.startswith("http") else "https://www.arken.dk"+href
        events.append(make_event(title, date_str,
                                 "ARKEN Museum for Samtidskunst",
                                 link, "ARKEN", "🖼️ Udstilling"))
    result = dedup(events)
    print(f"     ✓ {len(result)} udstillinger")
    return result


def scrape_charlottenborg():
    print("  → Kunsthal Charlottenborg...")
    soup = fetch("https://kunsthalcharlottenborg.dk/en/exhibitions/")
    if not soup: return []
    NAV = {"exhibitions","about","news","education","visit","press","en","da","shop"}
    events = []
    for a in soup.find_all("a", href=True):
        href  = a["href"]
        title = clean(a.get_text())
        if not title or title.lower() in NAV or is_garbage(title): continue
        if not any(kw in href for kw in ["/exhibitions/","/udstillinger/"]): continue
        if href.rstrip("/").endswith(("/exhibitions","/udstillinger")): continue
        link = href if href.startswith("http") else "https://kunsthalcharlottenborg.dk"+href
        events.append(make_event(title,"Løbende","Kunsthal Charlottenborg",
                                 link,"Charlottenborg","🖼️ Udstilling"))
    result = dedup(events)
    print(f"     ✓ {len(result)} udstillinger")
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  SAML ALT
# ══════════════════════════════════════════════════════════════════════════════

async def run_playwright():
    all_pw = []
    print("\n🎭 PLAYWRIGHT SCRAPERS...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Koncerter
        all_pw += await scrape_dr(browser, "1210", "DR Symfoniorkestret")
        all_pw += await scrape_dr(browser, "1211", "DR Big Band")
        all_pw += await scrape_tivoli(browser,"/program/sommerklassisk","Tivoli SommerKlassisk","🎻 Klassisk")
        all_pw += await scrape_tivoli(browser,"/program/jazz-i-tivoli","Jazz i Tivoli","🎷 Jazz")
        all_pw += await scrape_kgl_teater(browser)
        # Kirker
        for name, url in [
            ("Vor Frue Kirke","https://www.domkirken.dk/kalender/koncerter"),
            ("Vor Frelsers Kirke","https://www.vorfrelserskirke.dk/musikken-i-vor-frelsers-kirke/koncerter"),
            ("Holmens Kirke","https://www.holmenskirke.dk/det-sker/koncerter"),
            ("Christians Kirke","https://www.christianskirke.dk/det-sker/koncerter-og-musik"),
            ("Garnisonskirken","https://garnisonskirken.dk/kategori/arrangementer/koncerter/"),
        ]:
            all_pw += await scrape_church(browser, url, name)
        # Udstillinger
        all_pw += await scrape_smk(browser)
        all_pw += await scrape_glyptoteket(browser)
        await browser.close()
    print(f"   → Playwright total: {len(all_pw)} events")
    return all_pw

def collect_static():
    print("\n📄 STATISKE SCRAPERS...")
    events = []
    for fn in [scrape_copenhagenphil, scrape_mogensdahl,
               scrape_hirschsprung, scrape_copenhagencontemporary,
               scrape_designmuseum, scrape_thorvaldsen,
               scrape_arken, scrape_charlottenborg]:
        events += fn(); time.sleep(0.8)
    return events


# ══════════════════════════════════════════════════════════════════════════════
#  EMAIL – KALENDERDESIGN
# ══════════════════════════════════════════════════════════════════════════════

def generate_email(all_events):
    today  = datetime.now()
    wk     = today.isocalendar()[1]

    # Den kommende uge: næste mandag til søndag
    days_to_monday = (7 - today.weekday()) % 7
    if days_to_monday == 0: days_to_monday = 7
    week_start = (today + timedelta(days=days_to_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    week_end   = week_start + timedelta(days=6)
    date_range = f"{week_start.strftime('%-d. %b')}–{week_end.strftime('%-d. %b %Y')}"

    # Opdel i koncerter (har specifik dato) og udstillinger
    concerts    = []
    exhibitions = []
    for ev in all_events:
        if ev.get("category") == "🖼️ Udstilling":
            exhibitions.append(ev)
        elif parse_date(ev.get("date","")):
            concerts.append(ev)

    # ── SEKTION 1: Dag-for-dag koncerter ────────────────────────────────────
    concerts_by_day = defaultdict(list)
    for ev in concerts:
        dt = parse_date(ev["date"])
        if dt: concerts_by_day[dt.date()].append(ev)

    WEEKDAYS_DK = ["Mandag","Tirsdag","Onsdag","Torsdag","Fredag","Lørdag","Søndag"]
    concert_html = ""
    has_any_concert = False
    for i in range(7):
        day_dt   = (week_start + timedelta(days=i)).date()
        day_name = WEEKDAYS_DK[day_dt.weekday()]
        day_str  = f"{day_name} {day_dt.day}. {DANISH_MONTHS_SHORT[day_dt.month]}"
        day_evs  = concerts_by_day.get(day_dt, [])

        if day_evs:
            has_any_concert = True
            concert_html += f"""
            <tr>
              <td style="padding:10px 40px 3px 40px;">
                <span style="font-size:11px;font-weight:700;color:#c8a951;
                             letter-spacing:1.5px;text-transform:uppercase;
                             font-family:Arial,sans-serif;">{day_str}</span>
              </td>
            </tr>"""
            for ev in sorted(day_evs, key=lambda e: e.get("time","")):
                t    = ev.get("time","")
                cat  = "🎻" if ev.get("category") == "🎻 Klassisk" else "🎷"
                desc = ev.get("description","")
                concert_html += f"""
            <tr>
              <td style="padding:3px 40px 3px 52px;">
                <table width="100%" cellpadding="0" cellspacing="0" border="0"
                       style="border-left:2px solid #c8a951;padding-left:10px;
                              margin-bottom:4px;">
                  <tr><td>
                    <a href="{ev['link']}" style="font-size:14px;font-weight:700;
                       color:#1a1a1a;text-decoration:none;font-family:Georgia,serif;">
                      {cat} {ev['title']}</a><br>
                    <span style="font-size:12px;color:#666;font-family:Arial,sans-serif;">
                      {('⏰ '+t+' &nbsp; ') if t else ''}📍 {ev['venue']}
                    </span>
                    {f'<br><span style="font-size:11px;color:#888;font-family:Arial,sans-serif;">{desc}</span>' if desc else ''}
                  </td></tr>
                </table>
              </td>
            </tr>"""
        else:
            concert_html += f"""
            <tr>
              <td style="padding:6px 40px 3px 40px;">
                <span style="font-size:11px;color:#bbb;font-family:Arial,sans-serif;">
                  {day_str} &nbsp;—&nbsp; ingen arrangementer fundet
                </span>
              </td>
            </tr>"""

    if not has_any_concert:
        concert_html += """
            <tr><td style="padding:16px 40px;">
              <p style="margin:0;font-size:13px;color:#999;font-style:italic;
                        font-family:Arial,sans-serif;">
                Ingen scrapede koncerter fundet for denne uge.
                Tjek DR, Tivoli og kirkernes hjemmesider manuelt.
              </p>
            </td></tr>"""

    # ── SEKTION 2: Udstillinger grupperet per museum ────────────────────────
    by_source = defaultdict(list)
    for ex in exhibitions:
        by_source[ex.get("source", ex.get("venue","Øvrigt"))].append(ex)

    SOURCE_ORDER = ["SMK","Glyptoteket","Hirschsprung","ARKEN","CC",
                    "Thorvaldsen","Designmuseum","Charlottenborg"]
    ordered_sources = SOURCE_ORDER + [s for s in by_source if s not in SOURCE_ORDER]

    exhibition_html = ""
    for src in ordered_sources:
        if src not in by_source: continue
        exs = by_source[src]
        # Museum-overskrift
        venue_name = exs[0].get("venue", src)
        exhibition_html += f"""
            <tr>
              <td style="padding:14px 40px 4px 40px;">
                <p style="margin:0;font-size:12px;font-weight:700;color:#1a1a1a;
                          font-family:Arial,sans-serif;text-transform:uppercase;
                          letter-spacing:1px;border-bottom:1px solid #e8e4db;
                          padding-bottom:4px;">
                  <a href="{exs[0]['link']}" style="color:#1a1a1a;text-decoration:none;">
                    📍 {venue_name}
                  </a>
                </p>
              </td>
            </tr>"""
        for ex in exs[:8]:  # Max 8 per museum for overskuelighed
            date_disp = ex.get("date","Løbende")
            desc      = ex.get("description","")
            exhibition_html += f"""
            <tr>
              <td style="padding:2px 40px 2px 52px;">
                <span style="display:block;font-size:13px;color:#333;
                             font-family:Georgia,serif;line-height:1.5;">
                  <a href="{ex['link']}" style="color:#1a1a1a;text-decoration:none;
                     font-weight:600;">{ex['title']}</a>
                  {f'<span style="color:#c8a951;font-size:11px;"> · {date_disp}</span>' if date_disp != "Løbende" else ''}
                  {f'<br><span style="font-size:11px;color:#888;font-family:Arial,sans-serif;">{desc}</span>' if desc else ''}
                </span>
              </td>
            </tr>"""
        if len(exs) > 8:
            exhibition_html += f"""
            <tr><td style="padding:2px 40px 2px 52px;">
              <span style="font-size:11px;color:#999;font-family:Arial,sans-serif;">
                + {len(exs)-8} flere udstillinger
              </span>
            </td></tr>"""

    n_concerts  = len(concerts)
    n_exhibits  = len(exhibitions)

    return f"""<!DOCTYPE html><html lang="da">
<head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Kulturkalender – Uge {wk}</title></head>
<body style="margin:0;padding:0;background:#f0ede6;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:#f0ede6;padding:30px 15px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" border="0"
       style="max-width:600px;background:#fff;border-radius:4px;
              box-shadow:0 2px 20px rgba(0,0,0,0.08);">

  <!-- HEADER -->
  <tr><td style="background:#1a1a1a;padding:32px 40px 24px 40px;">
    <p style="margin:0;font-size:10px;color:#c8a951;letter-spacing:3px;
              text-transform:uppercase;font-family:Arial,sans-serif;">
      UGE {wk} &nbsp;·&nbsp; {date_range}</p>
    <h1 style="margin:8px 0 4px 0;font-size:30px;color:#fff;
               font-weight:400;">Kulturkalender</h1>
    <p style="margin:0;font-size:12px;color:#888;font-family:Arial,sans-serif;">
      {n_concerts} koncerter &nbsp;·&nbsp; {n_exhibits} udstillinger</p>
  </td></tr>

  <!-- SEKTION: KONCERTER -->
  <tr><td style="padding:24px 40px 8px 40px;background:#f8f6f1;
                  border-bottom:1px solid #e8e4db;">
    <p style="margin:0;font-size:12px;font-weight:700;color:#1a1a1a;
              letter-spacing:2px;text-transform:uppercase;
              font-family:Arial,sans-serif;">🎻 🎷 &nbsp;UGENS KONCERTER</p>
  </td></tr>
  {concert_html}

  <!-- SEKTION: UDSTILLINGER -->
  <tr><td style="padding:24px 40px 8px 40px;background:#f8f6f1;
                  border-top:2px solid #e8e4db;border-bottom:1px solid #e8e4db;
                  margin-top:16px;">
    <p style="margin:0;font-size:12px;font-weight:700;color:#1a1a1a;
              letter-spacing:2px;text-transform:uppercase;
              font-family:Arial,sans-serif;">🖼️ &nbsp;AKTUELLE UDSTILLINGER</p>
  </td></tr>
  {exhibition_html}

  <!-- FOOTER -->
  <tr><td style="padding:20px 40px;background:#1a1a1a;margin-top:8px;">
    <p style="margin:0;font-size:11px;color:#555;text-align:center;
              font-family:Arial,sans-serif;line-height:1.8;">
      Næste udgave: <span style="color:#c8a951;">
      {(week_end+timedelta(days=1)).strftime('%-d. %B %Y')}</span>
    </p>
  </td></tr>
</table></td></tr></table>
</body></html>"""


def send_email(html):
    today = datetime.now()
    msg   = MIMEMultipart("alternative")
    msg["Subject"] = f"🎭 Kulturkalender · Uge {today.isocalendar()[1]} · København"
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        print(f"\n📧 Sender til {RECIPIENT}...")
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        s.login(GMAIL_USER, GMAIL_PASSWORD)
        s.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())
        s.quit()
        print("   ✅ Sendt!")
    except Exception as e:
        print(f"   ❌ Fejl: {e}")


def main():
    print("="*55)
    print("  KØBENHAVNS KULTURKALENDER  v6")
    print(f"  {datetime.now().strftime('%A %-d. %B %Y kl. %H:%M')}")
    print("="*55)

    static = collect_static()
    pw     = asyncio.run(run_playwright())
    all_ev = dedup(static + pw)

    concerts   = [e for e in all_ev if e.get("category") != "🖼️ Udstilling" and parse_date(e.get("date",""))]
    exhibitions = [e for e in all_ev if e.get("category") == "🖼️ Udstilling"]
    print(f"\n📊 {len(concerts)} koncerter · {len(exhibitions)} udstillinger")

    html = generate_email(all_ev)
    with open("preview.html","w",encoding="utf-8") as f: f.write(html)
    print("🔍 Preview → kør: open preview.html")
    send_email(html)
    print("\n✅ Færdig!")

if __name__ == "__main__":
    main()
