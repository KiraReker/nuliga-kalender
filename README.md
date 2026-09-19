# TC Holthausen – nuLiga Online-Kalender

Dieses Projekt erzeugt automatisch **einen abonnierbaren Kalender je Mannschaft**.

Beispiele für stabile URLs:

- `.../feeds/herren-1.ics`
- `.../feeds/herren-30-1.ics`
- `.../feeds/damen-1.ics`

Die URL bleibt gleich. Der Inhalt wechselt automatisch auf die nächste Saison:

- Januar bis Juni → Sommer des aktuellen Jahres
- Juli bis Dezember → Winter aktuelles Jahr / Folgejahr

## Einmalige Einrichtung mit GitHub Pages

1. Auf GitHub ein neues Repository anlegen, z. B. `nuliga-kalender`.
2. Den kompletten Inhalt dieses Ordners in das Repository hochladen.
3. In GitHub: **Settings → Pages → Source → GitHub Actions**.
4. Unter **Actions** den Workflow `nuLiga Kalender aktualisieren` einmal manuell starten.
5. Danach ist die Seite typischerweise erreichbar unter:
   `https://DEIN-GITHUB-NAME.github.io/nuliga-kalender/`

Der Workflow aktualisiert die Kalender danach automatisch täglich.

## WhatsApp

Nicht mehr die `.ics`-Datei senden, sondern den stabilen Link, z. B.:

`https://DEIN-GITHUB-NAME.github.io/nuliga-kalender/feeds/herren-30-1.ics`

Oder einfach die Startseite schicken. Dort kann jede Mannschaft ausgewählt werden.

## Google Kalender

Google Kalender kann öffentliche Internetkalender per URL abonnieren. Das Hinzufügen
eines neuen URL-Kalenders wird von Google offiziell in der Web-Version am Computer
unter **Weitere Kalender → + → Per URL** unterstützt. Danach erscheint der abonnierte
Kalender auch in der Google-Kalender-App.

## Apple / iPhone

Die generierte Startseite enthält zusätzlich `webcal://`-Links, die sich auf
Apple-Geräten direkt als Kalenderabo öffnen lassen.

## Lokal testen

```bash
pip install -r requirements.txt
python nuliga_online_kalender.py
```

Danach liegt die generierte Website im Ordner `docs/`.
