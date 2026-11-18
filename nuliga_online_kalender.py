#!/usr/bin/env python3
"""
nuLiga -> mehrere iCalendar-Dateien (.ics)

Das Skript liest die Mannschaftsübersicht eines Vereins aus und erstellt
für JEDE gemeldete Mannschaft der zwei neuesten Saisons mit veröffentlichten Terminen
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

# Wie viele aufeinanderfolgende Saisons sollen im Kalender bleiben?
#
# 2 bedeutet z. B.:
#   Sommer 2026 + Winter 2026/2027
#
# Sobald in nuLiga Termine für Sommer 2027 veröffentlicht sind:
#   Winter 2026/2027 + Sommer 2027
#
# Sobald später Winter 2027/2028 Termine veröffentlicht sind:
#   Sommer 2027 + Winter 2027/2028
SEASONS_TO_KEEP = 2

# Früheste Saison, die überhaupt in die Online-Kalender aufgenommen wird.
#
# Damit wird Sommer 2026 absichtlich NICHT übernommen.
# Ab hier läuft das rollierende System automatisch weiter:
#   Winter 2026/2027
#   Winter 2026/2027 + Sommer 2027
#   Sommer 2027 + Winter 2027/2028
#   usw.
MIN_SEASON = "Winter 2026/2027"

# Ausgabeordner für die öffentliche Website / GitHub Pages.
OUTPUT_FOLDER = "docs"

# Optional: eigene öffentliche Basis-URL.
# Normalerweise None lassen. Auf GitHub Pages wird die URL automatisch erkannt.
# Beispiel bei eigener Domain:
# PUBLIC_BASE_URL = "https://kalender.tc-holthausen.de"
PUBLIC_BASE_URL = None

# Optionaler Abo-Zaehler ueber GoatCounter.
#
# Einmal kostenlos bei https://www.goatcounter.com/ registrieren und
# hier NUR den eigenen Site-Code eintragen.
#
# Beispiel:
#   Deine Statistik-Seite ist:
#       https://tc-holthausen.goatcounter.com
#
#   Dann:
#       GOATCOUNTER_SITE_CODE = "tc-holthausen"
#
# None = Zaehler deaktiviert.
GOATCOUNTER_SITE_CODE = None

# Angenommene Dauer eines Mannschaftsspiels.
MATCH_DURATION_HOURS = 5

LOCAL_TZ = ZoneInfo("Europe/Berlin")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/140 Safari/537.36 "
        "nuLiga-calendar-export/3.2"
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


def goatcounter_site_code() -> str | None:
    """
    Liefert einen gueltigen GoatCounter-Site-Code oder None.

    Beispiel:
        "tc-holthausen"
    fuer:
        https://tc-holthausen.goatcounter.com
    """
    value = clean(GOATCOUNTER_SITE_CODE or "")

    if not value:
        return None

    if not re.fullmatch(r"[a-zA-Z0-9-]+", value):
        raise ValueError(
            "GOATCOUNTER_SITE_CODE darf nur Buchstaben, Zahlen "
            "und Bindestriche enthalten."
        )

    return value.lower()


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


def season_name_from_championship(championship: str) -> str | None:
    """
    Wandelt den championship-Parameter in einen einheitlichen Saisonnamen um.

    Beispiele:
        "Düsseldorf S 26"   -> "Sommer 2026"
        "TVN S 27"          -> "Sommer 2027"
        "Düsseldorf W 26/27" -> "Winter 2026/2027"
        "TVN W 27/28"        -> "Winter 2027/2028"
    """
    championship = clean(championship)

    match = re.search(r"\bS\s+(\d{2})\b", championship, re.I)
    if match:
        year = 2000 + int(match.group(1))
        return f"Sommer {year}"

    match = re.search(r"\bW\s+(\d{2})/(\d{2})\b", championship, re.I)
    if match:
        y1 = 2000 + int(match.group(1))
        y2 = 2000 + int(match.group(2))
        return f"Winter {y1}/{y2}"

    match = re.search(r"\bSommer\s+(\d{4})\b", championship, re.I)
    if match:
        return f"Sommer {int(match.group(1))}"

    match = re.search(r"\bWinter\s+(\d{4})/(\d{4})\b", championship, re.I)
    if match:
        return f"Winter {int(match.group(1))}/{int(match.group(2))}"

    return None


def season_sort_key(season_name: str) -> tuple[int, int]:
    """
    Chronologische Sortierung:
        Sommer 2026       -> (2026, 0)
        Winter 2026/2027  -> (2026, 1)
        Sommer 2027       -> (2027, 0)
    """
    summer = re.fullmatch(r"Sommer\s+(\d{4})", clean(season_name), re.I)
    if summer:
        return int(summer.group(1)), 0

    winter = re.fullmatch(
        r"Winter\s+(\d{4})/(\d{4})",
        clean(season_name),
        re.I,
    )
    if winter:
        return int(winter.group(1)), 1

    return 0, 0


def seasons_label(seasons: list[str]) -> str:
    return " + ".join(sorted(seasons, key=season_sort_key))



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
    Liest den tatsächlichen nuLiga-Spielnamen aus dem Mannschaftsportrait.

    Normale Mannschaft:
        Mannschaft | TC Holthausen 1

    Spielgemeinschaft:
        Mannschaft | TC Holthausen/DSC 1899 1

    Der Name wird nur intern für Heim/Auswärts benutzt. Der Kalendername
    bleibt z. B. "Junioren U18 1".
    """

    # 1) Klassische Tabellenstruktur.
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        values = [
            clean(c.get_text(" ", strip=True))
            for c in cells
            if clean(c.get_text(" ", strip=True))
        ]

        if len(values) >= 2:
            first = values[0].rstrip(":").strip().lower()
            if first == "mannschaft":
                candidate = clean(values[1])
                if candidate:
                    return candidate

        # Manche nuLiga-Versionen packen Label und Wert in dieselbe Zelle.
        row_text = clean(row.get_text(" ", strip=True))
        match = re.search(
            r"\bMannschaft\s*:?\s*(.+?)(?=\s+Liga\b|$)",
            row_text,
            re.I,
        )
        if match:
            candidate = clean(match.group(1))
            if candidate and candidate.lower() != "mannschaft":
                return candidate

    # 2) Label "Mannschaft" im DOM suchen und unmittelbar folgenden Text lesen.
    for node in soup.find_all(string=True):
        if clean(node).rstrip(":").lower() != "mannschaft":
            continue

        parent = node.parent

        # Folgende Geschwister / Elternelemente durchsuchen.
        for element in [parent, parent.parent if parent else None]:
            if element is None:
                continue

            value = clean(element.get_text(" ", strip=True))
            match = re.search(
                r"\bMannschaft\s*:?\s*(.+?)(?=\s+Liga\b|$)",
                value,
                re.I,
            )
            if match:
                candidate = clean(match.group(1))
                if candidate:
                    return candidate

    # 3) Sehr robuster Text-Fallback:
    # Alles zwischen "Mannschaft" und dem nächsten Feld "Liga".
    page_text = clean(soup.get_text(" ", strip=True))

    match = re.search(
        r"\bMannschaft\s*:?\s*(.+?)\s+Liga\s*:",
        page_text,
        re.I,
    )
    if not match:
        match = re.search(
            r"\bMannschaft\s*:?\s*(.+?)\s+Liga\b",
            page_text,
            re.I,
        )

    if match:
        candidate = clean(match.group(1))
        if candidate:
            return candidate

    # 4) Letzter Fallback: aus den Spielterminen das Team ermitteln,
    # das in fast jeder Begegnung vorkommt.
    heading = None
    for h in soup.find_all(["h2", "h3"]):
        if "Spieltermine" in clean(h.get_text(" ", strip=True)):
            heading = h
            break

    if heading is not None:
        table = heading.find_next("table")

        if table is not None:
            counts = {}

            for row in table.find_all("tr"):
                values = [
                    clean(cell.get_text(" ", strip=True))
                    for cell in row.find_all(["td", "th"])
                ]

                for value in values:
                    if (
                        value
                        and not DATETIME_RE.search(value)
                        and re.search(r"\s+\d+\s*$", value)
                        and value.lower() not in {
                            "offen",
                            "heimmannschaft",
                            "gastmannschaft",
                        }
                    ):
                        counts[value] = counts.get(value, 0) + 1

            if counts:
                candidate, count = max(
                    counts.items(),
                    key=lambda item: item[1],
                )
                if count >= 2:
                    return candidate

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


def _group_page_url_from_row(row, base_url: str) -> str | None:
    """
    Findet in einer Vereins-Tabellenzeile den Link zur nuLiga-Gruppe.

    nuLiga hat die Vereinsseite geaendert: Die Mannschaftsnamen sind dort
    nicht mehr zwingend direkt mit teamPortrait verlinkt. Der Gruppenname
    bleibt aber verlinkt. Ueber die Gruppenseite kann das Mannschaftsportrait
    weiterhin eindeutig gefunden werden.
    """
    candidates = []

    for a in row.find_all("a", href=True):
        href = a["href"]
        text = clean(a.get_text(" ", strip=True))
        lower_href = href.lower()
        lower_text = text.lower()

        # Dokument-/PDF-Links sind keine Gruppenseiten.
        if "nudokument" in lower_href or ".pdf" in lower_href:
            continue

        score = 0

        if "grouppage" in lower_href:
            score += 5
        if "group=" in lower_href:
            score += 3
        if "championship=" in lower_href:
            score += 2
        if "gr." in lower_text or "gruppe" in lower_text:
            score += 3

        if score:
            candidates.append(
                (score, urljoin(base_url, href))
            )

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def discover_registered_teams(
    session: requests.Session,
) -> tuple[str, list[dict]]:

    response = session.get(CLUB_TEAMS_URL, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    club_name = detect_club_name(soup)

    teams = []
    seen = set()

    def add_team(
        raw_label: str,
        team_url: str,
        season_name: str | None = None,
        section_name: str | None = None,
    ) -> None:
        raw_label = clean(raw_label)
        if not raw_label:
            return

        championship = championship_from_url(team_url)

        if not season_name:
            season_name = season_name_from_championship(
                championship
            )

        if not season_name:
            return

        team_id = team_id_from_url(team_url)
        key = team_id or team_url

        if key in seen:
            return

        seen.add(key)

        if not section_name:
            if championship:
                section_name = section_name_from_championship(
                    championship,
                    season_name,
                )
            else:
                section_name = season_name

        teams.append(
            {
                "season": season_name,
                "section": section_name,
                "label": normalize_team_label(raw_label),
                "raw_label": raw_label,
                "team_url": team_url,
                "team_id": team_id,
                "championship": championship,
            }
        )

    # ------------------------------------------------------------------
    # 1) Bisheriger Weg:
    # Direkt vorhandene Mannschaftsportrait-Links lesen.
    # Das bleibt erhalten, falls nuLiga sie wieder/teilweise ausliefert.
    # ------------------------------------------------------------------
    for a in soup.find_all("a", href=True):
        href = a["href"]

        if "teamPortrait" not in href:
            continue

        team_url = urljoin(response.url, href)
        championship = championship_from_url(team_url)

        if not championship:
            continue

        season_name = season_name_from_championship(
            championship
        )
        if not season_name:
            continue

        raw_label = clean(a.get_text(" ", strip=True))
        if not raw_label:
            continue

        add_team(
            raw_label,
            team_url,
            season_name=season_name,
        )

    # ------------------------------------------------------------------
    # 2) Neuer Fallback fuer die aktuelle nuLiga-Vereinsseite:
    #
    # Vereinsseite:
    #   Mannschaft | Mannschaftsfuehrer | Gruppe | ...
    #
    # Der Mannschaftsname ist ggf. nur Text. Der Gruppenname ist aber
    # verlinkt. Wir oeffnen diese Gruppe und suchen dort das passende
    # teamPortrait des eigenen Vereins.
    # ------------------------------------------------------------------
    for table in soup.find_all("table"):
        heading_text = previous_season_heading(table)
        season_from_heading = (
            extract_season_name(heading_text)
            if heading_text
            else None
        )

        if not season_from_heading:
            continue

        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if not cells:
                continue

            raw_label = clean(
                cells[0].get_text(" ", strip=True)
            )

            if not raw_label:
                continue

            if raw_label.lower() in {
                "mannschaft",
                "team",
            }:
                continue

            group_url = _group_page_url_from_row(
                row,
                response.url,
            )

            if not group_url:
                continue

            try:
                _portrait_name, team_url = resolve_team_portrait(
                    session,
                    club_name,
                    {
                        "group_url": group_url,
                        "label": raw_label,
                    },
                )
            except (RuntimeError, requests.RequestException) as exc:
                print(
                    "WARNUNG: Mannschaftsportrait konnte ueber "
                    f"die Gruppe fuer '{raw_label}' nicht aufgeloest "
                    f"werden: {exc}"
                )
                continue

            championship = (
                championship_from_url(team_url)
                or championship_from_url(group_url)
            )

            season_name = (
                season_name_from_championship(championship)
                or season_from_heading
            )

            section_name = (
                section_name_from_championship(
                    championship,
                    season_name,
                )
                if championship
                else season_name
            )

            add_team(
                raw_label,
                team_url,
                season_name=season_name,
                section_name=section_name,
            )

    if not teams:
        raise RuntimeError(
            "Auf der Vereinsseite wurden keine Mannschaften gefunden. "
            "Weder direkte Mannschaftsportraits noch ueber die "
            "verlinkten Gruppen konnten Mannschaften aufgeloest werden."
        )

    teams.sort(
        key=lambda item: (
            season_sort_key(item["season"]),
            item["section"].lower(),
            item["label"].lower(),
        )
    )

    return club_name, teams


def _load_team_page(
    session: requests.Session,
    team_url: str,
    page_cache: dict[str, str],
) -> str:
    if team_url not in page_cache:
        response = session.get(team_url, timeout=30)
        response.raise_for_status()
        page_cache[team_url] = response.text

    return page_cache[team_url]


def team_has_published_game_dates(
    session: requests.Session,
    team_url: str,
    page_cache: dict[str, str],
) -> bool:
    """
    True nur dann, wenn im Abschnitt 'Spieltermine' mindestens ein
    echtes Datum + Uhrzeit veröffentlicht ist.
    """
    html_text = _load_team_page(session, team_url, page_cache)
    soup = BeautifulSoup(html_text, "html.parser")

    heading = None
    for h in soup.find_all(["h2", "h3"]):
        if "Spieltermine" in clean(h.get_text(" ", strip=True)):
            heading = h
            break

    if heading is None:
        return False

    table = heading.find_next("table")
    if table is None:
        return False

    return bool(DATETIME_RE.search(clean(table.get_text(" ", strip=True))))


def select_active_seasons(
    session: requests.Session,
    teams: list[dict],
    page_cache: dict[str, str],
) -> list[str]:
    """
    Nimmt die neuesten SEASONS_TO_KEEP Saisons, für die in nuLiga
    tatsächlich schon mindestens ein Spieltermin veröffentlicht wurde.

    Dadurch erfolgt der Wechsel nicht am 1. Januar/1. Juli, sondern erst,
    sobald die nächste Saison echte Termine hat.
    """
    min_key = season_sort_key(MIN_SEASON)

    available = sorted(
        {
            team["season"]
            for team in teams
            if season_sort_key(team["season"]) >= min_key
        },
        key=season_sort_key,
        reverse=True,
    )

    selected = []

    for season in available:
        season_teams = [
            team for team in teams
            if team["season"] == season
        ]

        published = False

        for team in season_teams:
            try:
                if team_has_published_game_dates(
                    session,
                    team["team_url"],
                    page_cache,
                ):
                    published = True
                    break
            except requests.RequestException as exc:
                print(
                    f"WARNUNG: Konnte {team['label']} / {season} "
                    f"nicht auf Termine prüfen: {exc}"
                )

        if published:
            selected.append(season)

        if len(selected) >= SEASONS_TO_KEEP:
            break

    if not selected:
        raise RuntimeError(
            "Es wurde ab der Start-Saison "
            f"'{MIN_SEASON}' keine Saison mit veröffentlichten "
            "Spielterminen gefunden."
        )

    # Normalerweise sind es zwei. Falls nuLiga nur eine Saison mit Terminen
    # bereitstellt, veröffentlichen wir diese eine, statt den ganzen Lauf
    # scheitern zu lassen.
    return sorted(selected, key=season_sort_key)


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
    page_cache: dict[str, str],
) -> tuple[list[dict], str, str]:

    html_text = _load_team_page(session, team_url, page_cache)
    soup = BeautifulSoup(html_text, "html.parser")
    page_url = team_url

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
                team_urls[name] = urljoin(page_url, href)

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
            page_url,
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
                "season": season_name,
                "source_url": team_url,
                "own_team_name": own_team_name,
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
    team_display_name: str,
    season_names: list[str],
    games: list[dict],
) -> None:

    now_utc = datetime.now(timezone.utc)
    season_text = seasons_label(season_names)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//nuLiga Tennis Kalender Export//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(team_display_name)}",
        f"X-WR-CALDESC:{ics_escape('Spieltermine ' + team_display_name)}",
        "X-WR-TIMEZONE:Europe/Berlin",
        "X-PUBLISHED-TTL:PT6H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
    ]

    for game in sorted(games, key=lambda item: item["start"]):
        start_utc = game["start"].astimezone(timezone.utc)
        end_utc = game["end"].astimezone(timezone.utc)

        own_team_name = game["own_team_name"]
        team_url = game["source_url"]

        is_home = game["home"] == own_team_name
        label = "Heim" if is_home else "Auswärts"

        summary = (
            f"{team_display_name} {label}: "
            f"{game['home']} – {game['away']}"
        )

        description_parts = [
            f"nuLiga – {team_display_name}",
            f"Saison: {game['season']}",
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

        description = "\n".join(description_parts)

        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{make_uid(team_url, game)}",
            f"DTSTAMP:{now_utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"LAST-MODIFIED:{now_utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART:{start_utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{end_utc.strftime('%Y%m%dT%H%M%SZ')}",
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

    output_file.write_bytes(
        ("\r\n".join(lines) + "\r\n").encode("utf-8")
    )



# ===========================================================================
# ÖFFENTLICHE ABO-SEITE
# ===========================================================================

def create_public_index(
    output_root: Path,
    club_name: str,
    active_seasons: list[str],
    feeds: list[dict],
) -> None:
    """
    Erstellt docs/index.html mit einem abonnierbaren Link je Mannschaft.
    Die Feed-URLs bleiben über die Saison hinweg stabil.
    """
    base_url = get_public_base_url()
    generated = datetime.now(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")
    active_season_text = seasons_label(active_seasons)

    gc_code = goatcounter_site_code()

    goatcounter_script = ""
    if gc_code:
        goatcounter_script = (
            '<script '
            f'data-goatcounter="https://{gc_code}.goatcounter.com/count" '
            'defer src="https://gc.zgo.at/count.js"></script>'
        )

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
                <p>{feed['games']} Termine · {html.escape(active_season_text)}</p>
              </div>
              <div class="actions">
                <a
                  class="primary"
                  href="{html.escape(wcal_url, quote=True)}"
                  data-subscribe-team="{html.escape(feed['slug'], quote=True)}"
                  data-subscribe-name="{html.escape(feed['name'], quote=True)}">
                  Apple / Kalender abonnieren
                </a>
                <a
                  href="{html.escape(google_url, quote=True)}"
                  target="_blank"
                  rel="noopener"
                  data-subscribe-team="{html.escape(feed['slug'], quote=True)}"
                  data-subscribe-name="{html.escape(feed['name'], quote=True)}">
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
    <p>Im Kalender enthalten: <strong>{html.escape(active_season_text)}</strong></p>
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

{goatcounter_script}

<script>
function countCalendarSubscription(link) {{
  const team = link.dataset.subscribeTeam || "";
  const name = link.dataset.subscribeName || team;

  if (!team) return;

  // Pro Browser/Geraet und Mannschaft nur einmal zaehlen.
  // Kalender-Refreshes laufen direkt ueber die .ics-Datei und werden
  // deshalb NICHT erneut gezaehlt.
  const storageKey = "nuliga-calendar-subscribed:" + team;

  try {{
    if (window.localStorage.getItem(storageKey) === "1") {{
      return;
    }}
  }} catch (e) {{
    // Falls localStorage gesperrt ist, kann trotzdem gezaehlt werden.
  }}

  if (
    window.goatcounter &&
    typeof window.goatcounter.count === "function"
  ) {{
    window.goatcounter.count({{
      path: "abo/" + team,
      title: "Kalender-Abo: " + name,
      event: true,
      no_session: true
    }});

    try {{
      window.localStorage.setItem(storageKey, "1");
    }} catch (e) {{
      // Zaehler funktioniert auch ohne localStorage,
      // dann koennen Wiederholungsklicks allerdings erneut zaehlen.
    }}
  }}
}}

document.addEventListener("DOMContentLoaded", () => {{
  document
    .querySelectorAll("a[data-subscribe-team]")
    .forEach((link) => {{
      link.addEventListener("click", () => {{
        countCalendarSubscription(link);
      }});
    }});
}});

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
        f"{club_name} – {active_season_text}",
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

    club_name, all_teams = discover_registered_teams(
        session
    )

    page_cache = {}

    active_seasons = select_active_seasons(
        session,
        all_teams,
        page_cache,
    )

    teams = [
        team
        for team in all_teams
        if team["season"] in active_seasons
    ]

    print(f"\nVerein: {club_name}")
    print(
        "Im Kalender enthalten: "
        f"{seasons_label(active_seasons)}"
    )
    print(
        f"Gefundene Mannschafts-Saison-Kombinationen: "
        f"{len(teams)}\n"
    )

    script_dir = Path(__file__).resolve().parent
    output_root = script_dir / OUTPUT_FOLDER
    feeds_root = output_root / "feeds"

    if feeds_root.exists():
        shutil.rmtree(feeds_root)
    feeds_root.mkdir(parents=True, exist_ok=True)

    address_cache = {}
    groups = {}
    errors = []

    for index, team in enumerate(teams, start=1):
        print(
            f"[{index}/{len(teams)}] "
            f"{team['section']} – {team['label']}"
        )

        try:
            games, display_name, own_team_name = get_schedule_for_team(
                session=session,
                team_url=team["team_url"],
                team_label=team["label"],
                own_club_info_url=own_club_info_url,
                season_name=team["season"],
                address_cache=address_cache,
                page_cache=page_cache,
            )

            key = slugify(display_name)

            group = groups.setdefault(
                key,
                {
                    "name": display_name,
                    "games": [],
                    "seasons": set(),
                },
            )

            group["games"].extend(games)
            group["seasons"].add(team["season"])

            print(
                f"    {own_team_name}: "
                f"{len(games)} Termine"
            )

        except Exception as exc:
            print(f"    FEHLER: {exc}")
            errors.append(
                (team["season"], team["label"], str(exc))
            )

    created = []
    public_feeds = []

    for slug, group in sorted(
        groups.items(),
        key=lambda item: item[1]["name"].lower(),
    ):
        # Doppelte Spiele sicherheitshalber entfernen.
        unique_games = {}
        for game in group["games"]:
            key = (
                game["source_url"],
                game["home"],
                game["away"],
                game["start"],
            )
            unique_games[key] = game

        games = sorted(
            unique_games.values(),
            key=lambda game: game["start"],
        )

        filename = f"{slug}.ics"
        output_file = feeds_root / filename

        create_ics(
            output_file=output_file,
            team_display_name=group["name"],
            season_names=active_seasons,
            games=games,
        )

        print(
            f"    -> {group['name']}: "
            f"{len(games)} Termine aus "
            f"{seasons_label(sorted(group['seasons'], key=season_sort_key))}"
        )
        print(f"       {output_file}")

        created.append(output_file)
        public_feeds.append(
            {
                "name": group["name"],
                "slug": slug,
                "filename": filename,
                "games": len(games),
            }
        )

    create_public_index(
        output_root=output_root,
        club_name=club_name,
        active_seasons=active_seasons,
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
