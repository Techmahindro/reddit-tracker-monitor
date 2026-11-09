#!/usr/bin/env python3
import os
import re
import time
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

import requests

BASE = Path.home() / "reddit_tracker_monitor"
DB = BASE / "monitor.db"

TRACKERS = {
    "eMuwarez": [r"emuwarez"],
    "HD-Olimpo": [r"hd[\s-]?olimpo"],
    "HDZero": [r"hd[\s-]?zero"],
    "Milnueve": [r"milnueve", r"mil[\s-]?nueve"],
    "NOBS": [r"\bnobs\b"],
    "ParabellumHD": [r"parabellum[\s-]?hd"],
    "PuntoTorrent": [r"punto[\s-]?torrent"],
    "Torrenteros": [r"torrenteros", r"\bttr\b"],
}

OPEN_TERMS = re.compile(
    r"open[\s-]?signups?|"
    r"open[\s-]?registration|"
    r"registration[s]?\s+(?:are\s+)?open|"
    r"registr[oa]s?\s+abiert[oa]s?|"
    r"inscripciones?\s+abiertas?|"
    r"invites?\s+open|"
    r"invitaciones?\s+abiertas?|"
    r"solicitudes?\s+abiertas?|"
    r"reclutamiento\s+abierto",
    re.I,
)

NEGATIVE = re.compile(
    r"\b(closed|cerrado|cerrados|cerradas|"
    r"expired|ended|finished)\b",
    re.I,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("tracker-monitor")

session = requests.Session()
session.headers["User-Agent"] = (
    "linux:es.tracker.registration.monitor:v1.0 "
    "(personal monitoring)"
)


def database():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            id TEXT PRIMARY KEY,
            tracker TEXT,
            title TEXT,
            url TEXT,
            detected_at TEXT
        )
    """)
    con.commit()
    return con


def reddit_token():
    client = os.environ["REDDIT_CLIENT_ID"]
    secret = os.environ["REDDIT_CLIENT_SECRET"]

    r = session.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(client, secret),
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def reddit_search(token, query, subreddit=None):
    if subreddit:
        url = (
            f"https://oauth.reddit.com/r/"
            f"{subreddit}/search"
        )
    else:
        url = "https://oauth.reddit.com/search"

    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "q": query,
        "sort": "new",
        "limit": 100,
        "restrict_sr": bool(subreddit),
        "t": "week",
        "type": "link",
    }

    for attempt in range(3):
        r = session.get(
            url, headers=headers,
            params=params, timeout=30
        )

        if r.status_code == 429:
            wait = min(
                int(r.headers.get("Retry-After", 60)),
                300
            )
            log.warning("Reddit 429. Esperando %ss", wait)
            time.sleep(wait)
            continue

        r.raise_for_status()
        return [
            item["data"]
            for item in r.json()["data"]["children"]
        ]

    return []


def notify(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat:
        log.info("Telegram sin configurar:\n%s", message)
        return False

    r = session.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat,
            "text": message,
            "disable_web_page_preview": True
        },
        timeout=20,
    )
    r.raise_for_status()
    return True


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    con = database()

    token = reddit_token()

    queries = [
        '"open signup"',
        '"open registration"',
        '"registro abierto"',
        '"registros abiertos"',
        '"invitaciones abiertas"',
    ]

    # Búsquedas por nombres agrupados
    names = list(TRACKERS.keys())
    for i in range(0, len(names), 4):
        queries.append(
            " OR ".join(f'"{n}"' for n in names[i:i+4])
        )

    posts = {}

    for query in queries:
        try:
            results = reddit_search(token, query)
            for post in results:
                posts[post["id"]] = post
        except requests.RequestException as e:
            log.error("Error buscando %s: %s", query, e)

    # Búsqueda adicional en comunidades específicas
    for subreddit in ["OpenSignups", "OpenedSignups"]:
        try:
            for post in reddit_search(
                token, "signup OR open OR registration",
                subreddit
            ):
                posts[post["id"]] = post
        except requests.RequestException as e:
            log.warning("%s: %s", subreddit, e)

    alerts = 0

    for post in posts.values():
        title = post.get("title", "")
        body = post.get("selftext", "")
        content = title + "\n" + body

        if not OPEN_TERMS.search(content):
            continue

        if NEGATIVE.search(title):
            continue

        # Evitar anuncios antiguos
        age = time.time() - post.get("created_utc", 0)
        if age > 7 * 86400:
            continue

        matched = [
            name for name, patterns in TRACKERS.items()
            if any(re.search(p, content, re.I)
                   for p in patterns)
        ]

        if not matched:
            continue

        post_id = post["id"]
        if con.execute(
            "SELECT 1 FROM seen WHERE id=?",
            (post_id,)
        ).fetchone():
            continue

        link = (
            "https://www.reddit.com"
            + post.get("permalink", "")
        )

        message = (
            "🔔 POSIBLE APERTURA DE TRACKER\n\n"
            f"Tracker: {', '.join(matched)}\n"
            f"Publicación: {title}\n"
            f"Reddit: {link}\n\n"
            "⚠️ Pendiente de verificar en la web oficial."
        )

        # Guardar solo si se pudo notificar
        # o si Telegram todavía no está configurado.
        sent = notify(message)

        if sent or not os.environ.get("TELEGRAM_BOT_TOKEN"):
            con.execute(
                "INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?)",
                (
                    post_id,
                    ", ".join(matched),
                    title,
                    link,
                    datetime.now(timezone.utc).isoformat()
                )
            )
            con.commit()
            alerts += 1

    log.info(
        "Publicaciones analizadas: %s | Alertas: %s",
        len(posts), alerts
    )
    con.close()


if __name__ == "__main__":
    main()
