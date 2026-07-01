"""
supabase_backend.py — dostęp do Supabase dla headless backendu (webhook_server.py).

Wymagane zmienne środowiskowe (te same wartości co po stronie CRM/Vercel):
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY  (omija RLS, tylko backend, nigdy klient)
"""
import os
from functools import lru_cache

from supabase import create_client, Client

from scraper_core import DEFAULT_WEIGHTS

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# scraper_config seed — musi odzwierciedlać to, co CRM zapisuje z panelu Ustawień.
DEFAULT_SCRAPER_CONFIG = {
    "google_places_api_key": "",
    "max_results_per_query": 60,
    "request_delay_ms": 500,
    "scoring_weights": {
        "brak_strony": DEFAULT_WEIGHTS["brak_strony"],
        "strona_nie_dziala": DEFAULT_WEIGHTS["strona_nie_dziala"],
        "strona_dziala": DEFAULT_WEIGHTS["strona_dziala"],
        "niemobilna_bonus": DEFAULT_WEIGHTS["niemobilna_bonus"],
    },
    "scoring_rules_reviews": DEFAULT_WEIGHTS["reguly_opinii"],
    "scoring_rules_rating": DEFAULT_WEIGHTS["reguly_oceny"],
}


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY nie są ustawione")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def get_owner_id() -> str:
    """Solo-admin: jedyne konto w auth.users (ten sam wzorzec co selltic-crm)."""
    db = get_supabase()
    users = db.auth.admin.list_users()
    if not users:
        raise RuntimeError("Nie znaleziono właściciela (auth.users jest puste)")
    return users[0].id


def load_scraper_config(owner_id: str) -> dict:
    """Wczytuje scraper_config (key/value per owner) z Supabase, scalone z domyślnymi wartościami."""
    db = get_supabase()
    res = db.table("scraper_config").select("key,value").eq("owner", owner_id).execute()
    cfg = {k: v for k, v in DEFAULT_SCRAPER_CONFIG.items()}
    for row in res.data or []:
        cfg[row["key"]] = row["value"]
    return cfg


def scoring_weights_from_config(cfg: dict) -> dict:
    """Konwertuje scraper_config['scoring_weights'/'scoring_rules_*'] na format score_website()."""
    sw = cfg.get("scoring_weights") or DEFAULT_SCRAPER_CONFIG["scoring_weights"]
    return {
        "brak_strony": sw.get("brak_strony", DEFAULT_WEIGHTS["brak_strony"]),
        "strona_nie_dziala": sw.get("strona_nie_dziala", DEFAULT_WEIGHTS["strona_nie_dziala"]),
        "strona_dziala": sw.get("strona_dziala", DEFAULT_WEIGHTS["strona_dziala"]),
        "niemobilna_bonus": sw.get("niemobilna_bonus", DEFAULT_WEIGHTS["niemobilna_bonus"]),
        "reguly_opinii": cfg.get("scoring_rules_reviews") or DEFAULT_WEIGHTS["reguly_opinii"],
        "reguly_oceny": cfg.get("scoring_rules_rating") or DEFAULT_WEIGHTS["reguly_oceny"],
    }


def resolve_api_key(cfg: dict) -> str:
    """Klucz Google Places: scraper_config ma pierwszeństwo, GOOGLE_PLACES_API_KEY jako fallback."""
    return cfg.get("google_places_api_key") or os.environ.get("GOOGLE_PLACES_API_KEY", "")
