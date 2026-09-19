#!/usr/bin/env python3
"""
nuLiga -> mehrere iCalendar-Dateien (.ics)

Das Skript liest die Mannschaftsübersicht eines Vereins aus und erstellt
für JEDE gemeldete Mannschaft der automatisch ermittelten nächsten Saison
eine eigene, abonnierbare Kalenderdatei mit stabiler Online-URL.

Beispiel TC Holthausen:
    Kalender/
      Winter 2026-2027/
        Herren 1.ics
        Herren 2.ics
        Herren 30 1.ics
        Damen 1.ics
        Damen 30 1.ics
        ...

Sommer:
- Platzadresse wird bevorzugt
- Hallenadresse ist Fallback

Winter:
- Hallenadresse wird bevorzugt
- Platzadresse ist Fallback

Installation:
    pip install requests beautifulsoup4

Start:
    python nuliga_online_kalender.py
"""

import hashlib
import html
import os
import re
import shutil
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


# ===========================================================================
# EINSTELLUNGEN
# ===========================================================================

# Nur noch DIESE Vereins-Mannschaftsübersicht ist nötig.
CLUB_TEAMS_URL = (
    "https://tvn.liga.nu/cgi-bin/WebObjects/nuLigaTENDE.woa/wa/clubTeams"
    "?club=36043"
)

# Saison wird automatisch anhand des aktuellen Datums gewählt:
#
# Januar bis Juni  -> Sommer des aktuellen Jahres
# Juli bis Dezember -> Winter aktuelles Jahr / Folgejahr
#
# Beispiele:
#   Februar 2027  -> Sommer 2027
#   September 2026 -> Winter 2026/2027
#
# Für einen manuellen Test kannst du hier z. B. "Sommer 2027" eintragen.
# Normalerweise bleibt der Wert None.
SEASON_OVERRIDE = None

# Ausgabeordner für die öffentliche Website / GitHub Pages.
OUTPUT_FOLDER = "docs"

# Optional: eigene öffentliche Basis-URL.
# Normalerweise None lassen. Auf GitHub Pages wird die URL automatisch erkannt.
# Beispiel bei eigener Domain:
# PUBLIC_BASE_URL = "https://kalender.tc-holthausen.de"
PUBLIC_BASE_URL = None

# Angenommene Dauer eines Mannschaftsspiels.
MATCH_DURATION_HOURS = 5

LOCAL_TZ = ZoneInfo("Europe/Berlin")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/140 Safari/537.36 "
        "nuLiga-calendar-export/3.0"
    )
}

DATETIME_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\s+(\d{1,2}:\d{2})\b")
ZIP_RE = re.compile(r"\b\d{5}\b")
SEASON_RE = re.compile(r"(Sommer \d{4}|Winter \d{4}/\d{4})$", re.I)


# ===========================================================================
# ALLGEMEINE HILFSFUNKTIONEN
# ===========================================================================

def clean(text: str) -> str:
    return " ".join(str(text).replace("\xa0", " ").split())


def ics_escape(text: str) -> str:
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def fold_ics_line(line: str, limit: int = 73) -> list[str]:
    """Faltet iCalendar-Zeilen, ohne UTF-8-Zeichen zu zerschneiden."""
    result = []
    current = ""
    current_bytes = 0
    first_line = True

    for char in line:
        char_bytes = len(char.encode("utf-8"))
        max_bytes = limit if first_line else limit - 1

        if current and current_bytes + char_bytes > max_bytes:
            result.append(current if first_line else " " + current)
            current = char
            current_bytes = char_bytes
            first_line = False
        else:
            current += char
            current_bytes += char_bytes

    if current or not result:
        result.append(current if first_line else " " + current)

    return result


def slugify(value: str) -> str:
    """
    Stabile, URL-freundliche Feed-Namen:
        "Herren 30 1" -> "herren-30-1"
        "Juniorinnen U15 1" -> "juniorinnen-u15-1"
    """
    value = clean(value)
    value = value.replace("ß", "ss")
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value or "team"


def get_public_base_url() -> str:
    """
    Priorität:
    1. Umgebungsvariable PUBLIC_BASE_URL
    2. Python-Konstante PUBLIC_BASE_URL
    3. GitHub-Pages-URL automatisch aus GITHUB_REPOSITORY
    4. lokaler Platzhalter
    """
    env_url = clean(os.environ.get("PUBLIC_BASE_URL", ""))
    if env_url:
        return env_url.rstrip("/")

    if PUBLIC_BASE_URL:
        return clean(PUBLIC_BASE_URL).rstrip("/")

    repository = clean(os.environ.get("GITHUB_REPOSITORY", ""))
    if "/" in repository:
        owner, repo = repository.split("/", 1)
        if repo.lower() == f"{owner.lower()}.github.io":
            return f"https://{owner}.github.io"
        return f"https://{owner}.github.io/{repo}"

    return "https://DEIN-GITHUB-NAME.github.io/nuliga-kalender"


def webcal_url(https_url: str) -> str:
    if https_url.startswith("https://"):
        return "webcal://" + https_url[len("https://"):]
    if https_url.startswith("http://"):
        return "webcal://" + https_url[len("http://"):]
    return https_url


def safe_filename(name: str) -> str:
    name = clean(name)
    name = re.sub(r'[<>:"/\\|?*]', "-", name)
    name = name.rstrip(" .")
    return name or "nuliga_kalender"


def safe_folder_name(name: str) -> str:
    return safe_filename(name.replace("/", "-"))


def normalize_team_label(value: str) -> str:
    """
    Aus:
        Herren 1 (4er)
        Herren 30 1 (4er)
        Damen 50 (4-er) 1

    wird:
        Herren 1
        Herren 30 1
        Damen 50 1
    """
    value = clean(value)
    value = re.sub(r"\s*\(4-?er\)\s*", " ", value, flags=re.I)
    return clean(value)


def get_squad_number(team_label: str) -> str | None:
    label = normalize_team_label(team_label)
    match = re.search(r"\b(\d+)\s*$", label)
    return match.group(1) if match else None


def get_target_season(now: datetime | None = None) -> str:
    """
    Liefert die Saison, die für die Vorbereitung als NÄCHSTE Saison gilt.

    Januar bis Juni:
        Sommer aktuelles Jahr

    Juli bis Dezember:
        Winter aktuelles Jahr / Folgejahr

    Beispiele:
        2027-02-10 -> "Sommer 2027"
        2026-09-19 -> "Winter 2026/2027"
    """
    if now is None:
        now = datetime.now(LOCAL_TZ)

    year = now.year

    if 1 <= now.month <= 6:
        return f"Sommer {year}"

    return f"Winter {year}/{year + 1}"


def extract_season_name(heading_text: str) -> str | None:
    """
    Extrahiert den eigentlichen Saisonanteil aus Überschriften wie:
        "TVN Winter 2026/2027"
        "Düsseldorf Winter 2026/2027"
        "Sommer 2027"

    Ergebnis:
        "Winter 2026/2027"
        "Sommer 2027"
    """
    match = SEASON_RE.search(clean(heading_text))
    return match.group(1) if match else None


def season_type_from_name(season_name: str) -> str:
    core = extract_season_name(season_name) or season_name
    if core.lower().startswith("winter"):
        return "winter"
    if core.lower().startswith("sommer"):
        return "summer"
    raise ValueError(f"Unbekannte Saison: {season_name}")


def google_maps_url(address: str) -> str:
    return "https://www.google.com/maps/search/?api=1&query=" + quote(address)


def base_club_name(club_name: str) -> str:
    """
    "TC Holthausen e.V." -> "TC Holthausen"
    """
    name = clean(club_name)
    name = re.sub(r"\s+e\.\s*V\.?\s*$", "", name, flags=re.I)
    name = re.sub(r"\s+\(.*?\)\s*$", "", name)
    return clean(name)


def derive_club_info_url(club_teams_url: str) -> str:
    parsed = urlparse(club_teams_url)
    club_id = parse_qs(parsed.query).get("club", [None])[0]

    if not club_id:
        raise RuntimeError(
            "In CLUB_TEAMS_URL wurde keine club=... ID gefunden."
        )

    return (
        f"{parsed.scheme}://{parsed.netloc}"
        "/cgi-bin/WebObjects/nuLigaTENDE.woa/wa/clubInfoDisplay"
        f"?club={club_id}"
    )


# ===========================================================================
# ADRESSEN
# ===========================================================================

def extract_address_from_text(text: str) -> str | None:
    raw_lines = [clean(line) for line in text.splitlines()]
    lines = []

    for line in raw_lines:
        if not line:
            continue

        lower = line.lower()

        if lower.startswith(("tel ", "tel.", "fax ", "mobil ")):
            continue
        if "@" in line:
            continue
        if lower.startswith(("http://", "https://", "www.")):
            continue
        if lower in {"hallenadresse", "platzadresse", "postadresse"}:
            continue

        line = re.sub(
            r"\s*\(Clubhaus[^)]*\)\s*$",
            "",
            line,
            flags=re.I,
        )
        lines.append(line)

    for line in lines:
        if ZIP_RE.search(line):
            return line

    for i, line in enumerate(lines):
        if ZIP_RE.search(line) and i > 0:
            return f"{lines[i - 1]}, {line}"

    return None


def address_block_from_table(soup: BeautifulSoup, label: str) -> str | None:
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if not cells:
            continue

        values = [clean(c.get_text(" ", strip=True)) for c in cells]

        label_index = next(
            (
                i
                for i, value in enumerate(values)
                if label.lower() in value.lower()
            ),
            None,
        )

        if label_index is None:
            continue

        for cell in cells[label_index + 1:]:
            address = extract_address_from_text(
                cell.get_text("\n", strip=True)
            )
            if address:
                return address

        row_text = row.get_text("\n", strip=True)
        address = extract_address_from_text(
            row_text.replace(label, "", 1)
        )
        if address:
            return address

    return None


def address_block_from_page_text(
    soup: BeautifulSoup,
    label: str,
) -> str | None:
    text = soup.get_text("\n", strip=True)

    labels = [
        "Platzadresse",
        "Hallenadresse",
        "Postadresse",
        "Routenplaner",
        "Mitgliederzahlen",
    ]

    start = text.lower().find(label.lower())
    if start < 0:
        return None

    end = len(text)

    for other in labels:
        if other.lower() == label.lower():
            continue

        pos = text.lower().find(
            other.lower(),
            start + len(label),
        )

        if pos >= 0:
            end = min(end, pos)

    return extract_address_from_text(
        text[start + len(label):end]
    )


def get_club_address(
    session: requests.Session,
    club_info_url: str,
    season_type: str,
) -> tuple[str | None, str | None]:

    response = session.get(club_info_url, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    def read(label: str) -> str | None:
        return (
            address_block_from_table(soup, label)
            or address_block_from_page_text(soup, label)
        )

    hall = read("Hallenadresse")
    court = read("Platzadresse")

    if season_type == "winter":
        if hall:
            return hall, "Hallenadresse"
        if court:
            return court, "Platzadresse"

    if season_type == "summer":
        if court:
            return court, "Platzadresse"
        if hall:
            return hall, "Hallenadresse"

    return None, None


def find_club_info_url(
    session: requests.Session,
    team_url: str,
) -> str | None:

    response = session.get(team_url, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a["href"]

        if "clubInfoDisplay" in href:
            return urljoin(response.url, href)

    return None


def route_link_from_row(row, base_url: str) -> str | None:
    for a in row.find_all("a", href=True):
        text = clean(a.get_text(" ", strip=True)).lower()

        if "routenplan" in text:
            return urljoin(base_url, a["href"])

    return None


# ===========================================================================
# VEREIN + MANNSCHAFTEN FINDEN
# ===========================================================================

def detect_club_name(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(["h1", "h2"]):
        value = clean(tag.get_text(" ", strip=True))

        if value and value.lower() not in {
            "mannschaften",
            "gruppeneinteilung",
        }:
            return value

    title = clean(soup.title.get_text(" ", strip=True)) if soup.title else ""

    match = re.search(r"nuLiga\s*[–-]\s*(.+?)\s*[–-]\s*Mannschaften", title)
    if match:
        return clean(match.group(1))

    raise RuntimeError("Vereinsname konnte nicht erkannt werden.")


def championship_from_url(url: str) -> str:
    """Liest den nuLiga-championship-Parameter aus einer URL."""
    params = parse_qs(urlparse(url).query)
    return clean(params.get("championship", [""])[0])


def team_id_from_url(url: str) -> str:
    """Liest die team= ID aus einer Mannschaftsportrait-URL."""
    params = parse_qs(urlparse(url).query)
    return clean(params.get("team", [""])[0])


def championship_matches_season(championship: str, target_season: str) -> bool:
    """
    Beispiele:
        championship = "Düsseldorf W 26/27"
        target       = "Winter 2026/2027"

        championship = "TVN S 27"
        target       = "Sommer 2027"
    """
    championship = clean(championship)
    target_season = clean(target_season)

    winter = re.fullmatch(r"Winter\s+(\d{4})/(\d{4})", target_season, re.I)
    if winter:
        y1 = int(winter.group(1))
        y2 = int(winter.group(2))
        short = rf"\bW\s+{y1 % 100:02d}/{y2 % 100:02d}\b"
        long = rf"\bWinter\s+{y1}/{y2}\b"
        return bool(
            re.search(short, championship, re.I)
            or re.search(long, championship, re.I)
        )

    summer = re.fullmatch(r"Sommer\s+(\d{4})", target_season, re.I)
    if summer:
        year = int(summer.group(1))
        short = rf"\bS\s+{year % 100:02d}\b"
        long = rf"\bSommer\s+{year}\b"
        return bool(
            re.search(short, championship, re.I)
            or re.search(long, championship, re.I)
        )

    return False


def section_name_from_championship(
    championship: str,
    target_season: str,
) -> str:
    """
    "TVN W 26/27"        -> "TVN Winter 2026/2027"
    "Düsseldorf W 26/27" -> "Düsseldorf Winter 2026/2027"
    """
    championship = clean(championship)

    prefix = championship
    prefix = re.sub(
        r"\s+(?:W\s+\d{2}/\d{2}|S\s+\d{2})\s*$",
        "",
        prefix,
        flags=re.I,
    )
    prefix = re.sub(
        r"\s+(?:Winter\s+\d{4}/\d{4}|Sommer\s+\d{4})\s*$",
        "",
        prefix,
        flags=re.I,
    )
    prefix = clean(prefix)

    return f"{prefix} {target_season}".strip()


def detect_own_team_name(soup: BeautifulSoup) -> str:
    """
    Liest im Mannschaftsportrait z. B.
        Mannschaft | TC Holthausen 1
    aus.
    """
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        values = [clean(c.get_text(" ", strip=True)) for c in cells]

        if len(values) >= 2 and values[0].lower() == "mannschaft":
            if values[1]:
                return values[1]

    # Fallback für leicht abweichendes HTML.
    page_text = clean(soup.get_text(" ", strip=True))
    match = re.search(
        r"\bMannschaft\s+((?:TC|TSC|TUS|TuS|SV|DJK|DSC|TSG|ETB|RW|BW)\b.+?\s+\d+)\b",
        page_text,
        re.I,
    )
    if match:
        return clean(match.group(1))

    raise RuntimeError(
        "Der nuLiga-Mannschaftsname konnte im Mannschaftsportrait "
        "nicht automatisch erkannt werden."
    )


def previous_season_heading(table) -> str | None:
    heading = table.find_previous(
        lambda tag:
            getattr(tag, "name", None) in {"h1", "h2", "h3", "h4"}
            and bool(SEASON_RE.search(clean(tag.get_text(" ", strip=True))))
    )

    if not heading:
        return None

    return clean(heading.get_text(" ", strip=True))


def discover_registered_teams(
    session: requests.Session,
    target_season: str,
) -> tuple[str, list[dict]]:

    response = session.get(CLUB_TEAMS_URL, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    club_name = detect_club_name(soup)

    teams = []
    seen_team_ids = set()
    seen_urls = set()

    # Robust gegen Änderungen am Tabellenlayout:
    # Die Mannschaftsnamen auf clubTeams sind selbst teamPortrait-Links.
    for a in soup.find_all("a", href=True):
        href = a["href"]

        if "teamPortrait" not in href:
            continue

        team_url = urljoin(response.url, href)
        championship = championship_from_url(team_url)

        if not championship:
            continue

        if not championship_matches_season(
            championship,
            target_season,
        ):
            continue

        raw_label = clean(a.get_text(" ", strip=True))
        if not raw_label:
            continue

        team_id = team_id_from_url(team_url)

        # team= ist die beste eindeutige ID. Falls sie einmal fehlt,
        # wird wenigstens die komplette URL dedupliziert.
        if team_id:
            if team_id in seen_team_ids:
                continue
            seen_team_ids.add(team_id)
        else:
            if team_url in seen_urls:
                continue
            seen_urls.add(team_url)

        teams.append(
            {
                "season": target_season,
                "section": section_name_from_championship(
                    championship,
                    target_season,
                ),
                "label": normalize_team_label(raw_label),
                "raw_label": raw_label,
                "team_url": team_url,
                "team_id": team_id,
                "championship": championship,
            }
        )

    if not teams:
        # Hilfreiche Diagnose, falls nuLiga das URL-Schema ändert.
        championships = sorted(
            {
                championship_from_url(
                    urljoin(response.url, a["href"])
                )
                for a in soup.find_all("a", href=True)
                if "teamPortrait" in a["href"]
                and championship_from_url(
                    urljoin(response.url, a["href"])
                )
            }
        )

        available = ", ".join(championships) or "keine"

        raise RuntimeError(
            f"Keine Mannschaften für '{target_season}' gefunden. "
            f"Auf der Vereinsseite erkannte Wettbewerbe: {available}"
        )

    teams.sort(
        key=lambda item: (
            item["section"].lower(),
            item["label"].lower(),
        )
    )

    return club_name, teams


def resolve_team_portrait(
    session: requests.Session,
    club_name: str,
    team: dict,
) -> tuple[str, str]:

    response = session.get(team["group_url"], timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    club_base = base_club_name(club_name)
    squad_number = get_squad_number(team["label"])

    candidates = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]

        if "teamPortrait" not in href:
            continue

        name = clean(a.get_text(" ", strip=True))

        if not name:
            continue

        # z.B. "TC Holthausen 1"
        if not name.lower().startswith(club_base.lower()):
            continue

        url = urljoin(response.url, href)
        candidates[(name, url)] = None

    candidate_list = list(candidates.keys())

    if squad_number:
        numbered = [
            item
            for item in candidate_list
            if re.search(
                rf"\s{re.escape(squad_number)}\s*$",
                item[0],
            )
        ]

        if len(numbered) == 1:
            return numbered[0]

        if numbered:
            candidate_list = numbered

    if len(candidate_list) == 1:
        return candidate_list[0]

    if not candidate_list:
        raise RuntimeError(
            f"Kein Mannschaftsportrait für '{team['label']}' "
            f"in der Gruppe gefunden."
        )

    # Wenn mehrere gleiche Teamnamen mit unterschiedlichen Session-URLs
    # vorhanden sind, bevorzugen wir eindeutige team= IDs.
    by_team_id = {}

    for name, url in candidate_list:
        team_id = parse_qs(urlparse(url).query).get("team", [url])[0]
        by_team_id.setdefault(team_id, (name, url))

    if len(by_team_id) == 1:
        return next(iter(by_team_id.values()))

    raise RuntimeError(
        f"Mannschaftsportrait für '{team['label']}' ist nicht eindeutig."
    )


# ===========================================================================
# SPIELPLAN EINER MANNSCHAFT
# ===========================================================================

def detect_team_display_name(
    soup: BeautifulSoup,
    fallback: str,
) -> str:

    season_word = r"(?:Sommer|Winter)"
    season_year = r"\d{4}(?:/\d{4})?"

    pattern = re.compile(
        rf"^\s*(.+?)\s*,\s*{season_word}\s+{season_year}\s*$",
        re.I,
    )

    for tag in soup.find_all(["h1", "h2", "h3", "h4", "div", "td", "span"]):
        value = clean(tag.get_text(" ", strip=True))

        match = pattern.match(value)

        if match:
            result = normalize_team_label(match.group(1))

            # Falls Vereinsname samt Nummer vorangestellt ist:
            # "TC Holthausen e.V. (3036) Herren 1" -> "Herren 1"
            prefixed = re.search(r"\(\s*\d+\s*\)\s*(.+)$", result)

            if prefixed:
                result = clean(prefixed.group(1))

            if result:
                return result

    return normalize_team_label(fallback)


def get_venue_for_game(
    session: requests.Session,
    home: str,
    own_team_name: str,
    own_club_info_url: str,
    home_team_url: str | None,
    nuliga_route_url: str | None,
    address_cache: dict,
    season_type: str,
) -> dict:

    if home == own_team_name:
        club_info_url = own_club_info_url

    elif home_team_url:
        try:
            club_info_url = find_club_info_url(
                session,
                home_team_url,
            )
        except requests.RequestException as exc:
            print(
                f"      WARNUNG: Vereinsseite für {home} "
                f"nicht abrufbar: {exc}"
            )
            club_info_url = None

    else:
        club_info_url = None

    address = None
    address_type = None

    if club_info_url:
        cache_key = (club_info_url, season_type)

        if cache_key not in address_cache:
            try:
                address_cache[cache_key] = get_club_address(
                    session,
                    club_info_url,
                    season_type,
                )
            except requests.RequestException as exc:
                print(
                    f"      WARNUNG: Adresse für {home} "
                    f"nicht abrufbar: {exc}"
                )
                address_cache[cache_key] = (None, None)

        address, address_type = address_cache[cache_key]

    return {
        "address": address,
        "address_type": address_type,
        "maps_url": google_maps_url(address) if address else None,
        "nuliga_route_url": nuliga_route_url,
    }


def get_schedule_for_team(
    session: requests.Session,
    team_url: str,
    team_label: str,
    own_club_info_url: str,
    season_name: str,
    address_cache: dict,
) -> tuple[list[dict], str, str]:

    response = session.get(team_url, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    own_team_name = detect_own_team_name(soup)

    team_display_name = detect_team_display_name(
        soup,
        team_label,
    )

    season_type = season_type_from_name(season_name)

    heading = None

    for h in soup.find_all(["h2", "h3"]):
        if "Spieltermine" in clean(h.get_text(" ", strip=True)):
            heading = h
            break

    if heading is None:
        # Mannschaft ist gemeldet, aber es gibt eventuell noch keinen
        # veröffentlichten Spielplan.
        return [], team_display_name, own_team_name

    table = heading.find_next("table")

    if table is None:
        return [], team_display_name, own_team_name

    games = []

    for row in table.find_all("tr"):
        cells = [
            clean(td.get_text(" ", strip=True))
            for td in row.find_all(["td", "th"])
        ]

        if not cells:
            continue

        joined = " | ".join(cells)
        match = DATETIME_RE.search(joined)

        if not match:
            continue

        date_text, time_text = match.groups()

        dt_index = next(
            (
                i
                for i, value in enumerate(cells)
                if DATETIME_RE.search(value)
            ),
            None,
        )

        if dt_index is None:
            continue

        following = [
            value
            for value in cells[dt_index + 1:]
            if value
        ]

        if len(following) < 2:
            continue

        home = following[0]
        away = following[1]

        team_urls = {}
        team_link_names = []

        for a in row.find_all("a", href=True):
            href = a["href"]
            name = clean(a.get_text(" ", strip=True))

            if name and "teamPortrait" in href:
                team_link_names.append(name)
                team_urls[name] = urljoin(response.url, href)

        # nuLiga rendert das eigene Team teilweise nicht als Link.
        if own_team_name not in (home, away):
            nonempty = [
                v
                for v in cells
                if v and not DATETIME_RE.search(v)
            ]

            candidates = [
                v
                for v in nonempty
                if v == own_team_name or v in team_link_names
            ]

            if len(candidates) >= 2:
                home, away = candidates[0], candidates[1]

        if own_team_name not in (home, away):
            continue

        start = datetime.strptime(
            f"{date_text} {time_text}",
            "%d.%m.%Y %H:%M",
        ).replace(tzinfo=LOCAL_TZ)

        end = start + timedelta(
            hours=MATCH_DURATION_HOURS
        )

        status = cells[-1] if cells else ""

        if status in (home, away):
            status = ""

        route_url = route_link_from_row(
            row,
            response.url,
        )

        home_team_url = team_urls.get(home)

        venue = get_venue_for_game(
            session=session,
            home=home,
            own_team_name=own_team_name,
            own_club_info_url=own_club_info_url,
            home_team_url=home_team_url,
            nuliga_route_url=route_url,
            address_cache=address_cache,
            season_type=season_type,
        )

        games.append(
            {
                "start": start,
                "end": end,
                "home": home,
                "away": away,
                "status": status,
                **venue,
            }
        )

    unique = {}

    for game in games:
        key = (
            game["home"],
            game["away"],
            game["start"],
        )
        unique[key] = game

    return (
        sorted(
            unique.values(),
            key=lambda game: game["start"],
        ),
        team_display_name,
        own_team_name,
    )


# ===========================================================================
# ICS ERZEUGEN
# ===========================================================================

def make_uid(
    team_url: str,
    game: dict,
) -> str:

    raw = (
        f"{team_url}|"
        f"{game['home']}|"
        f"{game['away']}"
    )

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:24]

    return f"{digest}@nuliga-tennis"


def create_ics(
    output_file: Path,
    team_url: str,
    own_team_name: str,
    team_display_name: str,
    season_name: str,
    games: list[dict],
) -> None:

    now_utc = datetime.now(timezone.utc)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//nuLiga Tennis Kalender Export//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        (
            "X-WR-CALNAME:"
            f"{ics_escape(team_display_name)} – "
            f"{ics_escape(season_name)}"
        ),
        "X-WR-TIMEZONE:Europe/Berlin",
        "X-PUBLISHED-TTL:PT6H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
    ]

    for game in games:
        start_utc = game["start"].astimezone(
            timezone.utc
        )
        end_utc = game["end"].astimezone(
            timezone.utc
        )

        is_home = game["home"] == own_team_name
        label = "Heim" if is_home else "Auswärts"

        summary = (
            f"{team_display_name} {label}: "
            f"{game['home']} – {game['away']}"
        )

        description_parts = [
            f"nuLiga – {team_display_name}",
            f"{game['home']} – {game['away']}",
        ]

        if game.get("address"):
            kind = game.get("address_type") or "Spielort"
            description_parts.append(
                f"{kind}: {game['address']}"
            )

        if game.get("maps_url"):
            description_parts.append(
                f"Google Maps: {game['maps_url']}"
            )

        if game.get("nuliga_route_url"):
            description_parts.append(
                "nuLiga Routenplan: "
                f"{game['nuliga_route_url']}"
            )

        if game.get("status"):
            description_parts.append(
                f"Hinweis: {game['status']}"
            )

        description_parts.append(
            f"Quelle: {team_url}"
        )

        description = "\n".join(
            description_parts
        )

        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{make_uid(team_url, game)}",
            (
                "DTSTAMP:"
                f"{now_utc.strftime('%Y%m%dT%H%M%SZ')}"
            ),
            (
                "LAST-MODIFIED:"
                f"{now_utc.strftime('%Y%m%dT%H%M%SZ')}"
            ),
            (
                "DTSTART:"
                f"{start_utc.strftime('%Y%m%dT%H%M%SZ')}"
            ),
            (
                "DTEND:"
                f"{end_utc.strftime('%Y%m%dT%H%M%SZ')}"
            ),
            f"SUMMARY:{ics_escape(summary)}",
        ]

        if game.get("address"):
            event_lines.append(
                f"LOCATION:{ics_escape(game['address'])}"
            )

        event_lines.extend(
            [
                f"DESCRIPTION:{ics_escape(description)}",
                f"URL:{team_url}",
                "END:VEVENT",
            ]
        )

        for line in event_lines:
            lines.extend(fold_ics_line(line))

    lines.append("END:VCALENDAR")

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Exakt CRLF schreiben -> keine zusätzlichen Leerzeilen unter Windows.
    output_file.write_bytes(
        ("\r\n".join(lines) + "\r\n").encode("utf-8")
    )



# ===========================================================================
# ÖFFENTLICHE ABO-SEITE
# ===========================================================================

def create_public_index(
    output_root: Path,
    club_name: str,
    target_season: str,
    feeds: list[dict],
) -> None:
    """
    Erstellt docs/index.html mit einem abonnierbaren Link je Mannschaft.
    Die Feed-URLs bleiben über die Saison hinweg stabil.
    """
    base_url = get_public_base_url()
    generated = datetime.now(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")

    rows = []

    for feed in sorted(feeds, key=lambda x: x["name"].lower()):
        https_url = f"{base_url}/feeds/{feed['filename']}"
        wcal_url = webcal_url(https_url)

        # Dieser Google-Link ist ein Komfort-Link für die Web-Version.
        # Die Google-Kalender-App selbst unterstützt das Abonnieren per URL
        # auf Mobilgeräten nicht zuverlässig.
        google_url = (
            "https://calendar.google.com/calendar/render?cid="
            + quote(wcal_url, safe="")
        )

        rows.append(
            f"""
            <article class="team">
              <div>
                <h2>{html.escape(feed['name'])}</h2>
                <p>{feed['games']} Termine · {html.escape(target_season)}</p>
              </div>
              <div class="actions">
                <a class="primary" href="{html.escape(wcal_url, quote=True)}">
                  Apple / Kalender abonnieren
                </a>
                <a href="{html.escape(google_url, quote=True)}" target="_blank" rel="noopener">
                  Google Kalender (Web)
                </a>
                <button type="button" data-url="{html.escape(https_url, quote=True)}">
                  Kalender-Link kopieren
                </button>
              </div>
              <code>{html.escape(https_url)}</code>
            </article>
            """
        )

    page = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(club_name)} – Mannschaftskalender</title>
  <meta name="description" content="Abonnierbare Mannschaftskalender von {html.escape(club_name)}">
  <style>
    :root {{
      font-family: system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      color: #17202a;
      background: #f5f6f7;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; }}
    main {{
      max-width: 900px;
      margin: 0 auto;
      padding: 28px 16px 48px;
    }}
    header {{
      margin-bottom: 24px;
    }}
    h1 {{ margin-bottom: 6px; }}
    .note {{
      background: white;
      border: 1px solid #d8dde3;
      border-radius: 12px;
      padding: 14px 16px;
      margin: 18px 0 24px;
    }}
    .team {{
      background: white;
      border: 1px solid #d8dde3;
      border-radius: 14px;
      padding: 16px;
      margin: 12px 0;
    }}
    .team h2 {{ margin: 0 0 4px; font-size: 1.15rem; }}
    .team p {{ margin: 0; color: #5c6773; }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 14px 0 10px;
    }}
    a, button {{
      border: 1px solid #aeb7c2;
      border-radius: 9px;
      padding: 10px 12px;
      background: white;
      color: #17202a;
      text-decoration: none;
      font: inherit;
      cursor: pointer;
    }}
    .primary {{
      background: #17202a;
      color: white;
      border-color: #17202a;
    }}
    code {{
      display: block;
      padding: 9px 10px;
      background: #f1f3f5;
      border-radius: 8px;
      overflow-wrap: anywhere;
      font-size: .82rem;
    }}
    footer {{
      margin-top: 26px;
      color: #697581;
      font-size: .9rem;
    }}
  </style>
</head>
<body>
<main>
  <header>
    <h1>{html.escape(club_name)} – Mannschaftskalender</h1>
    <p>Aktuelle Ziel-Saison: <strong>{html.escape(target_season)}</strong></p>
  </header>

  <div class="note">
    <strong>Einmal abonnieren, danach bleibt der Link gleich.</strong>
    Das System aktualisiert den Inhalt automatisch auf die jeweils nächste
    Sommer- bzw. Wintersaison. Auf iPhone/Apple-Geräten kann der Webcal-Link
    direkt abonniert werden. Google Kalender unterstützt das Hinzufügen eines
    Internetkalenders per URL offiziell nur in der Web-Version am Computer.
  </div>

  {''.join(rows)}

  <footer>
    Aktualisiert: {generated}. Quelle der Spieltermine: nuLiga.
  </footer>
</main>

<script>
document.querySelectorAll("button[data-url]").forEach((button) => {{
  button.addEventListener("click", async () => {{
    const url = button.dataset.url;
    try {{
      await navigator.clipboard.writeText(url);
      const old = button.textContent;
      button.textContent = "Link kopiert ✓";
      setTimeout(() => button.textContent = old, 1800);
    }} catch (e) {{
      window.prompt("Kalender-Link kopieren:", url);
    }}
  }});
}});
</script>
</body>
</html>
"""

    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "index.html").write_text(
        page,
        encoding="utf-8",
        newline="\n",
    )
    (output_root / ".nojekyll").write_text("", encoding="utf-8")

    # Zusätzlich einfache Linkliste für WhatsApp/Copy&Paste.
    link_lines = [
        f"{club_name} – {target_season}",
        "",
    ]
    for feed in sorted(feeds, key=lambda x: x["name"].lower()):
        link_lines.append(
            f"{feed['name']}: {base_url}/feeds/{feed['filename']}"
        )

    (output_root / "kalender-links.txt").write_text(
        "\n".join(link_lines) + "\n",
        encoding="utf-8",
    )


# ===========================================================================
# HAUPTPROGRAMM
# ===========================================================================

def main():
    session = requests.Session()
    session.headers.update(HEADERS)

    own_club_info_url = derive_club_info_url(
        CLUB_TEAMS_URL
    )

    target_season = SEASON_OVERRIDE or get_target_season()

    club_name, teams = discover_registered_teams(
        session,
        target_season,
    )

    print(f"\nVerein: {club_name}")
    print(f"Nächste Saison automatisch gewählt: {target_season}")
    print(
        f"Gefundene Mannschaften: {len(teams)}\n"
    )

    script_dir = Path(__file__).resolve().parent
    output_root = script_dir / OUTPUT_FOLDER
    feeds_root = output_root / "feeds"

    # Alte Feed-Dateien entfernen, damit ausgeschiedene Mannschaften
    # nicht versehentlich weiter öffentlich angeboten werden.
    if feeds_root.exists():
        shutil.rmtree(feeds_root)
    feeds_root.mkdir(parents=True, exist_ok=True)

    address_cache = {}

    created = []
    public_feeds = []
    used_filenames = set()
    errors = []

    for index, team in enumerate(teams, start=1):
        print(
            f"[{index}/{len(teams)}] "
            f"{team['section']} – {team['label']}"
        )

        try:
            team_url = team["team_url"]

            games, display_name, own_team_name = get_schedule_for_team(
                session=session,
                team_url=team_url,
                team_label=team["label"],
                own_club_info_url=own_club_info_url,
                season_name=team["season"],
                address_cache=address_cache,
            )

            # Stabile URL unabhängig von Sommer/Winter.
            # Beispiel: /feeds/herren-30-1.ics
            base_slug = slugify(display_name)
            filename = f"{base_slug}.ics"

            # Sicherheitsnetz bei Namenskollisionen.
            if filename in used_filenames:
                suffix = team.get("team_id") or str(index)
                filename = f"{base_slug}-{suffix}.ics"

            used_filenames.add(filename)
            output_file = feeds_root / filename

            create_ics(
                output_file=output_file,
                team_url=team_url,
                own_team_name=own_team_name,
                team_display_name=display_name,
                season_name=team["season"],
                games=games,
            )

            print(
                f"    {own_team_name}: "
                f"{len(games)} Termine"
            )
            print(
                f"    -> {output_file}"
            )

            created.append(output_file)
            public_feeds.append(
                {
                    "name": display_name,
                    "filename": filename,
                    "games": len(games),
                }
            )

        except Exception as exc:
            print(
                f"    FEHLER: {exc}"
            )
            errors.append(
                (team["season"], team["label"], str(exc))
            )

    create_public_index(
        output_root=output_root,
        club_name=club_name,
        target_season=target_season,
        feeds=public_feeds,
    )

    print(f"\nÖffentliche Basis-URL: {get_public_base_url()}")
    print(f"Startseite: {get_public_base_url()}/")

    print("\n======================================")
    print(
        f"Fertig: {len(created)} Kalenderdateien erstellt."
    )

    if created:
        print(
            f"Ordner: {output_root.resolve()}"
        )

    if errors:
        print(
            f"\n{len(errors)} Mannschaft(en) "
            "konnten nicht verarbeitet werden:"
        )

        for season, label, error in errors:
            print(
                f"  - {season} / {label}: {error}"
            )


if __name__ == "__main__":
    main()
