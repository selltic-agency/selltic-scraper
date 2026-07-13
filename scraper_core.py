"""
scraper_core.py — logika scrapowania i scoringu, bez zależności od Streamlit.

Współdzielona przez:
- selltic_scraper.py (stary interfejs Streamlit)
- webhook_server.py  (nowy headless backend na Cloud Run)

Nie ma tu żadnych efektów ubocznych przy imporcie poza odczytem GCS_BUCKET
ze zmiennej środowiskowej.
"""
import os
import time

import requests

GCS_BUCKET = os.environ.get("GCS_BUCKET", "")


def gcs_client():
    from google.cloud import storage
    return storage.Client()


def gcs_upload_bytes(local_filename: str, data: bytes, content_type: str = "text/csv"):
    """Wgrywa bajty do bucketa GCS pod danym kluczem. No-op jeśli GCS_BUCKET nie ustawiony."""
    if not GCS_BUCKET:
        return
    client = gcs_client()
    blob = client.bucket(GCS_BUCKET).blob(local_filename)
    blob.upload_from_string(data, content_type=content_type)


# ── Scoring stron WWW ──────────────────────────────────────────────────────────

DEFAULT_WEIGHTS = {
    "brak_strony": 40,
    "strona_nie_dziala": 30,
    "strona_dziala": 0,
    "niemobilna_bonus": 10,
    "reguly_opinii": [
        {"min_count": 1, "points": 5},
        {"min_count": 15, "points": 12},
        {"min_count": 50, "points": 20},
    ],
    "reguly_oceny": [
        {"min_rating": 3.0, "points": 5},
        {"min_rating": 4.0, "points": 10},
        {"min_rating": 4.5, "points": 15},
    ],
}


def _match_rule(rules: list[dict], value: float, threshold_key: str) -> tuple[int, dict | None]:
    """Znajduje regułę z najwyższym progiem <= value. Zwraca (punkty, reguła) albo (0, None)."""
    best = None
    for rule in rules:
        try:
            threshold = float(rule[threshold_key])
        except (KeyError, ValueError, TypeError):
            continue
        if threshold <= value and (best is None or threshold > float(best[threshold_key])):
            best = rule
    if best is None:
        return 0, None
    return int(best["points"]), best


def score_website(url: str, rating, review_count, weights: dict) -> tuple[int, str, dict]:
    """
    Zwraca (lead_score, website_status, breakdown).
    website_status: 'brak' | 'nie_dziala' | 'dziala'
    breakdown: {klucz: {"punkty": int, "opis": str}}
    """
    w = weights
    breakdown = {}

    # URL bywa pustym stringiem lub samą spacją ("website" z Google Places) —
    # traktujemy to jak brak strony, a nie jak domenę do sprawdzenia.
    if isinstance(url, str):
        url = url.strip()

    if not url:
        website_status = "brak"
        score_website_pts = int(w["brak_strony"])
        breakdown["stan_strony"] = {"punkty": score_website_pts, "opis": "Brak strony/domeny"}
    else:
        # Uzupełnij brakujący schemat (np. "przyklad.pl" -> "https://przyklad.pl"),
        # inaczej requests rzuca MissingSchema i strona zawsze wygląda na "nie_dziala".
        if "://" not in url:
            url = "https://" + url
        try:
            resp = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
            if not resp.ok:
                website_status = "nie_dziala"
                score_website_pts = int(w["strona_nie_dziala"])
                breakdown["stan_strony"] = {"punkty": score_website_pts, "opis": "Jest domena, ale nie działa"}
            else:
                website_status = "dziala"
                score_website_pts = int(w["strona_dziala"])
                breakdown["stan_strony"] = {"punkty": score_website_pts, "opis": "Jest strona i działa"}
                html = resp.text[:20000]
                if 'name="viewport"' not in html.lower():
                    bonus = int(w["niemobilna_bonus"])
                    score_website_pts += bonus
                    breakdown["niemobilna"] = {"punkty": bonus, "opis": "Strona niemobilna (brak meta viewport)"}
        except Exception:
            website_status = "nie_dziala"
            score_website_pts = int(w["strona_nie_dziala"])
            breakdown["stan_strony"] = {"punkty": score_website_pts, "opis": "Jest domena, ale nie działa (błąd połączenia)"}

    try:
        rc = int(review_count) if review_count not in (None, "") else 0
    except (ValueError, TypeError):
        rc = 0
    try:
        rt = float(rating) if rating not in (None, "") else 0
    except (ValueError, TypeError):
        rt = 0

    opinie_pts, opinie_rule = _match_rule(w.get("reguly_opinii", []), rc, "min_count")
    if opinie_rule is not None:
        breakdown["opinie"] = {"punkty": opinie_pts, "opis": f"≥ {opinie_rule['min_count']} opinii"}

    ocena_pts, ocena_rule = _match_rule(w.get("reguly_oceny", []), rt, "min_rating")
    if ocena_rule is not None:
        breakdown["ocena"] = {"punkty": ocena_pts, "opis": f"≥ {ocena_rule['min_rating']} oceny"}

    total = score_website_pts + opinie_pts + ocena_pts
    # Wynik trzymamy w zakresie 0–100. Wagi są w pełni edytowalne z UI, więc ich
    # suma potrafi wyjść poza ten przedział, a kolumny lead_score w CRM
    # (prospects/deals) oraz cała prezentacja („X/100", kolory progów) zakładają
    # 0–100. Bez przycięcia wynik > 100 przechodził przez scraped_leads (bez
    # ograniczenia), a wywracał się dopiero przy „Przenieś do Prospectingu".
    total = max(0, min(100, total))
    return total, website_status, breakdown


def format_breakdown(breakdown: dict) -> list[str]:
    return [f"+{item['punkty']} pkt: {item['opis']}" for item in breakdown.values()]


# ── Google Places API ──────────────────────────────────────────────────────────

def text_search(query, api_key, page_token=None):
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": query, "key": api_key, "language": "pl"}
    if page_token:
        params["pagetoken"] = page_token
    return requests.get(url, params=params, timeout=10).json()


def text_search_page(query, api_key, page_token=None, *, sleep=time.sleep):
    """
    Pobiera jedną stronę wyników text search, odporny na dwie typowe przyczyny
    fałszywych błędów przy stronicowaniu:

    1. INVALID_REQUEST po podaniu next_page_token — token Google Places staje się
       aktywny z ZMIENNYM opóźnieniem (zwykle ~2 s, czasem więcej). Zapytanie zbyt
       wcześnie zwraca INVALID_REQUEST, mimo że słowo kluczowe i lokalizacja są
       poprawne (realna przyczyna błędu np. dla "fizjoterapeuta Wrocław", które ma
       wiele stron wyników). Ponawiamy z narastającym odstępem zamiast zgłaszać
       błąd zapytania.
    2. OVER_QUERY_LIMIT — chwilowy limit tempa; ponawiamy po krótkiej pauzie.

    Pierwsza strona (page_token=None) NIE jest ponawiana na INVALID_REQUEST — tam
    to naprawdę oznacza nieprawidłowe zapytanie i decyzję o błędzie podejmuje caller.

    `sleep` jest wstrzykiwalny, żeby testy nie musiały realnie czekać.
    """
    if not page_token:
        data = text_search(query, api_key, None)
        if data.get("status") == "OVER_QUERY_LIMIT":
            sleep(3)
            data = text_search(query, api_key, None)
        return data

    data = {}
    for delay in (2, 3, 4, 5, 6):
        sleep(delay)  # daj tokenowi czas na aktywację
        data = text_search(query, api_key, page_token)
        if data.get("status") not in ("INVALID_REQUEST", "OVER_QUERY_LIMIT"):
            return data
    return data  # wyczerpane ponowienia — decyzję o błędzie podejmuje caller


def place_details(place_id, api_key):
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": "name,formatted_phone_number,website,formatted_address,rating,user_ratings_total,business_status",
        "key": api_key,
        "language": "pl"
    }
    return requests.get(url, params=params, timeout=10).json().get("result", {})
