import streamlit as st
import requests
import pandas as pd
import time
import os
import json
from datetime import datetime
from io import BytesIO
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

st.set_page_config(page_title="Selltic Scraper", page_icon="🔍", layout="wide")

COLUMNS = ["place_id", "Nazwa", "Telefon", "Strona WWW", "Adres", "Ocena", "Liczba opinii", "Status", "Branża", "Miasto", "Data dodania", "Website Status", "Lead Score"]
EXPORT_COLUMNS = ["Nazwa", "Telefon", "Strona WWW", "Adres", "Ocena", "Liczba opinii", "Status", "Branża", "Miasto", "Data dodania", "Website Status", "Lead Score"]
MASTER_FILE = "baza_leadow.csv"
HISTORY_FILE = "historia_zapytan.json"
WEIGHTS_FILE = "scoring_weights.json"

# ── Hasło dostępu (Cloud Run: ustaw zmienną środowiskową APP_PASSWORD) ────────
# Wyjątek: jeśli aplikacja jest osadzona jako zakładka w CRM (iframe/reverse proxy),
# CRM przekazuje ?token=... zgodny z EMBED_TOKEN i logowanie jest pomijane,
# ponieważ użytkownik jest już uwierzytelniony po stronie CRM.

def check_password() -> bool:
    """Prosta bramka hasłem. Hasło bierze ze zmiennej środowiskowej APP_PASSWORD."""
    correct_password = os.environ.get("APP_PASSWORD", "")
    embed_token = os.environ.get("EMBED_TOKEN", "")

    if st.session_state.get("password_correct", False):
        return True

    url_token = st.query_params.get("token", "")
    if embed_token and url_token == embed_token:
        st.session_state["password_correct"] = True
        return True

    if not correct_password:
        # Brak ustawionego hasła w środowisku - nie blokuj (np. lokalny dev),
        # ale wypisz ostrzeżenie żeby nie zapomnieć go ustawić na produkcji.
        st.sidebar.warning("⚠️ APP_PASSWORD nie jest ustawione — aplikacja działa bez hasła!")
        return True

    def password_entered():
        if st.session_state.get("password_input") == correct_password:
            st.session_state["password_correct"] = True
            del st.session_state["password_input"]
        else:
            st.session_state["password_correct"] = False

    st.title("🔒 Selltic Scraper")
    st.text_input("Hasło dostępu", type="password", on_change=password_entered, key="password_input")
    if st.session_state.get("password_correct") is False:
        st.error("Niepoprawne hasło")
    return False


if not check_password():
    st.stop()


# ── Trwałość danych w Google Cloud Storage (opcjonalne, ale zalecane na Cloud Run) ──
# Cloud Run nie ma trwałego dysku - bez tego baza_leadow.csv i historia_zapytan.json
# znikną przy każdym restarcie/redeployu/skalowaniu kontenera.
# Ustaw zmienną środowiskową GCS_BUCKET, aby włączyć automatyczny zapis/odczyt z bucketa.

GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
_gcs_synced = False


def _gcs_client():
    from google.cloud import storage
    return storage.Client()


def gcs_pull_all():
    """Pobiera baza_leadow.csv i historia_zapytan.json z bucketa, jeśli istnieją."""
    global _gcs_synced
    if not GCS_BUCKET or _gcs_synced:
        return
    try:
        client = _gcs_client()
        bucket = client.bucket(GCS_BUCKET)
        for fname in (MASTER_FILE, HISTORY_FILE, WEIGHTS_FILE):
            blob = bucket.blob(fname)
            if blob.exists():
                blob.download_to_filename(fname)
    except Exception as e:
        st.sidebar.error(f"Błąd pobierania danych z GCS: {e}")
    _gcs_synced = True


def gcs_push(local_filename: str):
    """Wysyła pojedynczy plik do bucketa po zapisie lokalnym."""
    if not GCS_BUCKET:
        return
    try:
        client = _gcs_client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(local_filename)
        if os.path.exists(local_filename):
            blob.upload_from_filename(local_filename)
    except Exception as e:
        st.sidebar.error(f"Błąd zapisu danych do GCS: {e}")


gcs_pull_all()


# ── Scoring stron WWW ──────────────────────────────────────────────────────────

DEFAULT_WEIGHTS = {
    "brak_strony": 40,
    "strona_nie_dziala": 35,
    "brak_viewport": 10,
    "brak_ssl": 8,
    "wolna_strona_pkt": 5,
    "wolna_strona_prog_sek": 3.0,
    "uboga_tresc": 7,
    "opinie_50_plus": 20,
    "opinie_15_49": 12,
    "opinie_1_14": 5,
    "ocena_4_5_plus": 15,
    "ocena_4_0_4_5": 10,
    "ocena_3_0_4_0": 5,
}


def load_weights() -> dict:
    """Wczytuje wagi scoringu z pliku, a jeśli brak - zapisuje i zwraca domyślne."""
    if os.path.exists(WEIGHTS_FILE):
        try:
            with open(WEIGHTS_FILE, "r", encoding="utf-8") as f:
                weights = json.load(f)
            merged = dict(DEFAULT_WEIGHTS)
            merged.update(weights)
            return merged
        except Exception:
            pass
    save_weights(DEFAULT_WEIGHTS)
    return dict(DEFAULT_WEIGHTS)


def save_weights(weights: dict):
    with open(WEIGHTS_FILE, "w", encoding="utf-8") as f:
        json.dump(weights, f, ensure_ascii=False, indent=2)
    gcs_push(WEIGHTS_FILE)


def score_website(url: str, rating, review_count, weights: dict = None) -> tuple[int, str, dict]:
    """
    Zwraca (lead_score 0-100, website_status, breakdown).
    website_status: 'brak' | 'aktywna' | 'nie_dziala' | 'wolna'
    """
    w = weights if weights is not None else load_weights()
    breakdown = {}

    # Komponent A: obecność strony (0-40 pkt)
    if not url:
        breakdown["brak_strony"] = w["brak_strony"]
        website_status = "brak"
        score_a = w["brak_strony"]
    else:
        try:
            resp = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
            elapsed = resp.elapsed.total_seconds()
            html = resp.text[:20000] if resp.ok else ""
            if not resp.ok:
                breakdown["strona_nie_dziala"] = w["strona_nie_dziala"]
                website_status = "nie_dziala"
                score_a = w["strona_nie_dziala"]
            else:
                website_status = "wolna" if elapsed > w["wolna_strona_prog_sek"] else "aktywna"
                score_a = 0
        except Exception:
            breakdown["strona_nie_dziala"] = w["strona_nie_dziala"]
            website_status = "nie_dziala"
            score_a = w["strona_nie_dziala"]
            html, elapsed = "", None

    # Komponent B: jakość istniejącej, działającej strony (0-25 pkt)
    score_b = 0
    if url and website_status in ("aktywna", "wolna"):
        if 'name="viewport"' not in html.lower():
            score_b += w["brak_viewport"]
            breakdown["brak_viewport"] = w["brak_viewport"]
        if url.startswith("http://"):
            score_b += w["brak_ssl"]
            breakdown["brak_ssl"] = w["brak_ssl"]
        if elapsed and elapsed > w["wolna_strona_prog_sek"]:
            score_b += w["wolna_strona_pkt"]
            breakdown["wolna_strona"] = w["wolna_strona_pkt"]
        if len(html) < 5000 or "<title" not in html.lower():
            score_b += w["uboga_tresc"]
            breakdown["uboga_tresc"] = w["uboga_tresc"]
        score_b = min(score_b, 25)

    # Komponent C: potencjał firmy wg opinii Google (0-35 pkt)
    score_c = 0
    try:
        rc = int(review_count) if review_count not in (None, "") else 0
    except (ValueError, TypeError):
        rc = 0
    try:
        rt = float(rating) if rating not in (None, "") else 0
    except (ValueError, TypeError):
        rt = 0

    if rc >= 50:
        score_c += w["opinie_50_plus"]
        breakdown["opinie"] = w["opinie_50_plus"]
    elif rc >= 15:
        score_c += w["opinie_15_49"]
        breakdown["opinie"] = w["opinie_15_49"]
    elif rc >= 1:
        score_c += w["opinie_1_14"]
        breakdown["opinie"] = w["opinie_1_14"]
    if rt >= 4.5:
        score_c += w["ocena_4_5_plus"]
        breakdown["ocena"] = w["ocena_4_5_plus"]
    elif rt >= 4.0:
        score_c += w["ocena_4_0_4_5"]
        breakdown["ocena"] = w["ocena_4_0_4_5"]
    elif rt >= 3.0:
        score_c += w["ocena_3_0_4_0"]
        breakdown["ocena"] = w["ocena_3_0_4_0"]

    total = min(score_a + score_b + score_c, 100)
    return total, website_status, breakdown


BREAKDOWN_LABELS = {
    "brak_strony": "Brak strony WWW",
    "strona_nie_dziala": "Strona nie odpowiada (timeout/błąd)",
    "brak_viewport": "Brak tagu viewport (strona niemobilna)",
    "brak_ssl": "Brak SSL (http://)",
    "wolna_strona": "Wolno działająca strona (>{}s)".format(DEFAULT_WEIGHTS["wolna_strona_prog_sek"]),
    "uboga_tresc": "Uboga treść / brak tytułu strony",
    "opinie": "Potencjał wg liczby opinii Google",
    "ocena": "Potencjał wg oceny Google",
}


def format_breakdown(breakdown: dict) -> list[str]:
    return [f"+{pts} pkt: {BREAKDOWN_LABELS.get(key, key)}" for key, pts in breakdown.items()]


# ── Integracja z CRM (selltic-crm) ──────────────────────────────────────────────
# Wymagane zmienne środowiskowe: CRM_API_BASE_URL, SCRAPER_IMPORT_KEY
# (ta sama wartość SCRAPER_IMPORT_KEY musi być ustawiona też w Vercel po stronie CRM)

CRM_API_BASE_URL = os.environ.get("CRM_API_BASE_URL", "").rstrip("/")
SCRAPER_IMPORT_KEY = os.environ.get("SCRAPER_IMPORT_KEY", "")
CRM_ENABLED = bool(CRM_API_BASE_URL and SCRAPER_IMPORT_KEY)


def crm_check_existing(place_ids: list[str]) -> set[str]:
    """Pyta CRM które z podanych place_id już istnieją. Zwraca pusty set przy błędzie/braku konfiguracji."""
    if not CRM_ENABLED or not place_ids:
        return set()
    try:
        resp = requests.get(
            f"{CRM_API_BASE_URL}/api/prospecting/existing-ids",
            params={"place_ids": ",".join(place_ids)},
            headers={"X-API-Key": SCRAPER_IMPORT_KEY},
            timeout=10,
        )
        if resp.ok:
            return set(resp.json().get("existing", []))
    except Exception as e:
        st.sidebar.warning(f"⚠️ Nie udało się sprawdzić duplikatów w CRM: {e}")
    return set()


def crm_import_batch(rows: list[dict]) -> dict | None:
    """Wysyła paczkę leadów do CRM. Zwraca odpowiedź albo None przy błędzie/braku konfiguracji."""
    if not CRM_ENABLED or not rows:
        return None
    payload = [
        {
            "place_id": r["place_id"],
            "name": r["Nazwa"],
            "phone": r["Telefon"] or None,
            "website": None if r["Strona WWW"] == "BRAK" else r["Strona WWW"],
            "address": r["Adres"],
            "rating": r["Ocena"] or None,
            "review_count": r["Liczba opinii"] or None,
            "business_status": r["Status"],
            "industry": r["Branża"],
            "city": r["Miasto"],
            "lead_score": r.get("Lead Score"),
            "website_status": r.get("Website Status"),
        }
        for r in rows
    ]
    try:
        resp = requests.post(
            f"{CRM_API_BASE_URL}/api/prospecting/import",
            json=payload,
            headers={"X-API-Key": SCRAPER_IMPORT_KEY},
            timeout=30,
        )
        if resp.ok:
            return resp.json()
        st.sidebar.error(f"❌ CRM odpowiedział błędem: {resp.status_code}")
    except Exception as e:
        st.sidebar.error(f"❌ Nie udało się wysłać leadów do CRM: {e}")
    return None


# ── Master DB helpers ─────────────────────────────────────────────────────────

def load_master() -> pd.DataFrame:
    if os.path.exists(MASTER_FILE):
        df = pd.read_csv(MASTER_FILE, dtype=str).fillna("")
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[COLUMNS]
    return pd.DataFrame(columns=COLUMNS)


def save_master(df: pd.DataFrame):
    df.to_csv(MASTER_FILE, index=False, encoding="utf-8-sig")
    gcs_push(MASTER_FILE)


def append_to_master(rows: list[dict]) -> tuple[int, int]:
    """Append new rows to master, deduplicate by place_id. Returns (added, skipped)."""
    df = load_master()
    existing_ids = set(df["place_id"].tolist())
    new_rows, skipped = [], 0
    for row in rows:
        if row["place_id"] in existing_ids:
            skipped += 1
        else:
            existing_ids.add(row["place_id"])
            new_rows.append(row)
    if new_rows:
        df = pd.concat([df, pd.DataFrame(new_rows, columns=COLUMNS)], ignore_index=True)
        save_master(df)
    return len(new_rows), skipped


# ── History helpers ───────────────────────────────────────────────────────────

def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_query_to_history(branza: str, lok: str, leady: int):
    history = load_history()
    key = f"{branza.lower().strip()}|{lok.lower().strip()}"
    history[key] = {"data": datetime.now().strftime("%Y-%m-%d %H:%M"), "leady": leady}
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    gcs_push(HISTORY_FILE)


def qkey(branza: str, lok: str) -> str:
    return f"{branza.lower().strip()}|{lok.lower().strip()}"


# ── API helpers ───────────────────────────────────────────────────────────────

def text_search(query, api_key, page_token=None):
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": query, "key": api_key, "language": "pl"}
    if page_token:
        params["pagetoken"] = page_token
    return requests.get(url, params=params, timeout=10).json()


def place_details(place_id, api_key):
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": "name,formatted_phone_number,website,formatted_address,rating,user_ratings_total,business_status",
        "key": api_key,
        "language": "pl"
    }
    return requests.get(url, params=params, timeout=10).json().get("result", {})


# ── Excel export ──────────────────────────────────────────────────────────────

def to_excel(df: pd.DataFrame) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Leady"
    header_fill = PatternFill("solid", fgColor="1A3ADB")
    header_font = Font(bold=True, color="FFFFFF", name="Arial", size=10)
    border = Border(
        left=Side(style="thin", color="DDDDDD"),
        right=Side(style="thin", color="DDDDDD"),
        bottom=Side(style="thin", color="DDDDDD"),
    )
    cols = list(df.columns)
    for ci, col in enumerate(cols, 1):
        cell = ws.cell(row=1, column=ci, value=col)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for ri, row in enumerate(df.itertuples(index=False), 2):
        for ci, val in enumerate(row, 1):
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.font = Font(name="Arial", size=10)
            cell.border = border
            if ri % 2 == 0:
                cell.fill = PatternFill("solid", fgColor="F0F4FF")
    col_widths = {"Nazwa": 30, "Telefon": 18, "Strona WWW": 35, "Adres": 40,
                  "Ocena": 10, "Liczba opinii": 14, "Status": 18, "Branża": 16,
                  "Miasto": 16, "Data dodania": 18}
    for ci, col in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(ci)].width = col_widths.get(col, 15)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ══════════════════════════════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════════════════════════════

st.title("🔍 Selltic – Google Maps Scraper")

with st.sidebar:
    st.header("⚙️ Konfiguracja")
    env_api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if env_api_key:
        api_key = env_api_key
        st.success("✅ Klucz Google Places API wczytany ze zmiennej środowiskowej")
    else:
        api_key = st.text_input("Google Places API Key", type="password", placeholder="AIza...")
    st.divider()
    if CRM_ENABLED:
        st.success("✅ Połączono z CRM (auto-import leadów)")
    else:
        st.info("ℹ️ CRM nieskonfigurowany — leady zostają tylko lokalnie/w GCS")
    st.divider()
    master_df = load_master()
    st.metric("📦 Leadów w bazie", len(master_df))
    st.metric("📋 Wykonanych zapytań", len(load_history()))
    st.divider()
    st.markdown("💡 **$200 free/miesiąc** ≈ 4 000 firm")

tab_scraper, tab_baza, tab_historia, tab_test_scoring, tab_konfig_scoring = st.tabs(
    ["🚀 Scraper", "📦 Baza leadów", "📋 Historia zapytań", "🧪 Test scoringu", "⚙️ Konfiguracja scoringu"]
)


# ── TAB 1: Scraper ────────────────────────────────────────────────────────────
with tab_scraper:
    history = load_history()
    master_ids = set(load_master()["place_id"].tolist())

    col1, col2 = st.columns(2)
    with col1:
        branze_input = st.text_area("Branże (jedna na linię)",
            placeholder="hydraulik\nusługi hydrauliczne\ninstalator wod-kan", height=150)
    with col2:
        lokalizacje_input = st.text_area("Lokalizacje (jedna na linię)",
            placeholder="Wrocław Krzyki\nWrocław Fabryczna\nWrocław Śródmieście", height=150)

    col3, col4 = st.columns(2)
    with col3:
        tylko_bez_strony = st.checkbox("Tylko firmy BEZ strony WWW", value=True)
    with col4:
        max_strony = st.slider("Max stron wyników na zapytanie", 1, 3, 2)

    # Query preview
    if branze_input.strip() and lokalizacje_input.strip():
        branze_prev = [b.strip() for b in branze_input.strip().splitlines() if b.strip()]
        lok_prev = [l.strip() for l in lokalizacje_input.strip().splitlines() if l.strip()]
        nowe, zrobione = [], []
        for b in branze_prev:
            for l in lok_prev:
                k = qkey(b, l)
                if k in history:
                    zrobione.append(f"{b} · {l} — {history[k]['data']}, {history[k]['leady']} leadów")
                else:
                    nowe.append(f"{b} · {l}")
        if zrobione:
            with st.expander(f"⚠️ {len(zrobione)} zapytań już wykonanych – zostaną pominięte"):
                for z in zrobione:
                    st.markdown(f"- ~~{z}~~")
        if nowe:
            with st.expander(f"✅ {len(nowe)} nowych zapytań", expanded=True):
                for n in nowe:
                    st.markdown(f"- {n}")

    powtorz = st.checkbox("Wykonaj ponownie już zrobione zapytania", value=False)

    if st.button("🚀 Rozpocznij scraping", type="primary", use_container_width=True):
        if not api_key:
            st.error("Wklej klucz API w panelu bocznym.")
        elif not branze_input.strip() or not lokalizacje_input.strip():
            st.error("Uzupełnij branże i lokalizacje.")
        else:
            branze = [b.strip() for b in branze_input.strip().splitlines() if b.strip()]
            lokalizacje = [l.strip() for l in lokalizacje_input.strip().splitlines() if l.strip()]
            pairs = [(b, l) for b in branze for l in lokalizacje
                     if qkey(b, l) not in history or powtorz]

            if not pairs:
                st.warning("Wszystkie zapytania już wykonane. Zaznacz 'Wykonaj ponownie' żeby powtórzyć.")
                st.stop()

            st.info(f"💾 Wyniki trafią do **{MASTER_FILE}** · do wykonania: **{len(pairs)} zapytań**")
            progress_bar = st.progress(0)
            status_text = st.empty()
            m1, m2, m3, m4 = st.columns(4)
            met_dodane = m1.empty()
            met_duplikaty = m2.empty()
            met_query = m3.empty()
            met_pominiete = m4.empty()
            table_placeholder = st.empty()

            session_rows = []
            seen_this_session = set()
            pominiete = 0
            duplikaty_baza = 0

            for pair_nr, (branza, lok) in enumerate(pairs, 1):
                query = f"{branza} {lok}"
                next_token = None
                leady_dla_query = 0

                for page_nr in range(max_strony):
                    status_text.info(f"⏳ {pair_nr}/{len(pairs)}: **{query}** – strona {page_nr + 1}")
                    if page_nr > 0 and next_token:
                        for i in range(2, 0, -1):
                            status_text.info(f"⏳ **{query}** – czekam {i}s...")
                            time.sleep(1)

                    try:
                        data = text_search(query, api_key, next_token if page_nr > 0 else None)
                    except Exception as e:
                        status_text.error(f"❌ Błąd: {e}")
                        break

                    api_status = data.get("status")
                    if api_status == "REQUEST_DENIED":
                        status_text.error("❌ REQUEST_DENIED – sprawdź klucz API i rozliczenia.")
                        st.stop()
                    elif api_status in ("ZERO_RESULTS",):
                        break
                    elif api_status != "OK":
                        status_text.warning(f"⚠️ {api_status}: {data.get('error_message','')}")
                        break

                    results = data.get("results", [])

                    # Dedup względem CRM (jedno zapytanie na całą stronę wyników)
                    crm_existing_ids = crm_check_existing([p["place_id"] for p in results])

                    for i, place in enumerate(results):
                        pid = place["place_id"]
                        status_text.info(f"⏳ **{query}** – {i+1}/{len(results)}: *{place.get('name','')}*")

                        # Dedup within session
                        if pid in seen_this_session:
                            pominiete += 1
                            met_pominiete.metric("⏭️ Duplikaty sesji", pominiete)
                            continue
                        seen_this_session.add(pid)

                        # Dedup against master DB
                        if pid in master_ids:
                            duplikaty_baza += 1
                            met_duplikaty.metric("🗄️ Już w bazie", duplikaty_baza)
                            continue

                        # Dedup against CRM (oszczędza wywołania place_details/Places API)
                        if pid in crm_existing_ids:
                            duplikaty_baza += 1
                            met_duplikaty.metric("🗄️ Już w bazie", duplikaty_baza)
                            continue

                        try:
                            details = place_details(pid, api_key)
                        except Exception:
                            continue
                        time.sleep(0.05)

                        strona_www = details.get("website", "")
                        if tylko_bez_strony and strona_www:
                            pominiete += 1
                            met_pominiete.metric("⏭️ Duplikaty sesji", pominiete)
                            continue

                        ocena = details.get("rating", "")
                        liczba_opinii = details.get("user_ratings_total", "")
                        lead_score, website_status, _breakdown = score_website(strona_www, ocena, liczba_opinii)

                        row = {
                            "place_id": pid,
                            "Nazwa": details.get("name", ""),
                            "Telefon": details.get("formatted_phone_number", ""),
                            "Strona WWW": strona_www if strona_www else "BRAK",
                            "Adres": details.get("formatted_address", ""),
                            "Ocena": ocena,
                            "Liczba opinii": liczba_opinii,
                            "Status": details.get("business_status", ""),
                            "Branża": branza,
                            "Miasto": lok,
                            "Data dodania": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "Website Status": website_status,
                            "Lead Score": lead_score,
                        }

                        session_rows.append(row)
                        master_ids.add(pid)
                        leady_dla_query += 1

                        # Append to master immediately
                        df_new = pd.DataFrame([row], columns=COLUMNS)
                        if os.path.exists(MASTER_FILE):
                            df_new.to_csv(MASTER_FILE, mode="a", header=False, index=False, encoding="utf-8-sig")
                        else:
                            df_new.to_csv(MASTER_FILE, index=False, encoding="utf-8-sig")
                        gcs_push(MASTER_FILE)

                        met_dodane.metric("✅ Dodano do bazy", len(session_rows))
                        met_duplikaty.metric("🗄️ Już w bazie", duplikaty_baza)
                        met_query.metric("🔍 Zapytanie", f"{pair_nr}/{len(pairs)}")
                        met_pominiete.metric("⏭️ Duplikaty sesji", pominiete)
                        table_placeholder.dataframe(
                            pd.DataFrame(session_rows)[EXPORT_COLUMNS],
                            use_container_width=True, height=350
                        )

                    next_token = data.get("next_page_token")
                    if not next_token:
                        break

                save_query_to_history(branza, lok, leady_dla_query)
                progress_bar.progress(pair_nr / len(pairs))

            progress_bar.progress(1.0)
            status_text.success(f"✅ Gotowe! Dodano **{len(session_rows)}** nowych leadów · pominięto **{duplikaty_baza}** już istniejących w bazie.")

            if session_rows:
                df_session = pd.DataFrame(session_rows)[EXPORT_COLUMNS]
                excel_buf = to_excel(df_session)
                st.download_button(
                    label="📥 Pobierz Excel (tylko ta sesja)",
                    data=excel_buf,
                    file_name=f"leady_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                    type="primary"
                )

                # Automatyczny import do CRM
                if CRM_ENABLED:
                    with st.spinner("📤 Wysyłam leady do CRM..."):
                        result = crm_import_batch(session_rows)
                    if result is not None:
                        st.success(
                            f"📤 CRM: dodano **{result.get('added', '?')}**, "
                            f"zaktualizowano **{result.get('updated', '?')}**."
                        )
                        st.session_state.pop("crm_retry_rows", None)
                    else:
                        st.session_state["crm_retry_rows"] = session_rows
                        st.error("❌ Import do CRM nie powiódł się. Możesz spróbować ponownie poniżej.")

    # Przycisk ponowienia importu, jeśli ostatnia automatyczna wysyłka się nie powiodła
    if CRM_ENABLED and st.session_state.get("crm_retry_rows"):
        if st.button("🔁 Spróbuj ponownie wysłać do CRM", use_container_width=True):
            with st.spinner("📤 Wysyłam leady do CRM..."):
                result = crm_import_batch(st.session_state["crm_retry_rows"])
            if result is not None:
                st.success(f"📤 CRM: dodano **{result.get('added', '?')}**, zaktualizowano **{result.get('updated', '?')}**.")
                st.session_state.pop("crm_retry_rows", None)
            else:
                st.error("❌ Nadal nie udało się połączyć z CRM.")


# ── TAB 2: Baza leadów ────────────────────────────────────────────────────────
with tab_baza:
    df_master = load_master()

    if df_master.empty:
        st.info("Baza jest pusta. Uruchom scraper żeby zebrać pierwsze leady.")
    else:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Wszystkich leadów", len(df_master))
        col2.metric("Unikalnych miast", df_master["Miasto"].nunique())
        col3.metric("Unikalnych branż", df_master["Branża"].nunique())
        col4.metric("Bez strony WWW", (df_master["Strona WWW"] == "BRAK").sum())

        st.divider()

        # Filters
        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            miasta = ["Wszystkie"] + sorted(df_master["Miasto"].unique().tolist())
            filtr_miasto = st.selectbox("Miasto", miasta)
        with fc2:
            branze_list = ["Wszystkie"] + sorted(df_master["Branża"].unique().tolist())
            filtr_branza = st.selectbox("Branża", branze_list)
        with fc3:
            filtr_strona = st.selectbox("Strona WWW", ["Wszystkie", "Tylko BEZ strony", "Tylko Z stroną"])

        df_filtered = df_master.copy()
        if filtr_miasto != "Wszystkie":
            df_filtered = df_filtered[df_filtered["Miasto"] == filtr_miasto]
        if filtr_branza != "Wszystkie":
            df_filtered = df_filtered[df_filtered["Branża"] == filtr_branza]
        if filtr_strona == "Tylko BEZ strony":
            df_filtered = df_filtered[df_filtered["Strona WWW"] == "BRAK"]
        elif filtr_strona == "Tylko Z stroną":
            df_filtered = df_filtered[df_filtered["Strona WWW"] != "BRAK"]

        st.markdown(f"**{len(df_filtered)} rekordów** po filtrach")
        st.dataframe(df_filtered[EXPORT_COLUMNS], use_container_width=True, height=450, hide_index=True)

        bc1, bc2, bc3 = st.columns(3)
        with bc1:
            excel_all = to_excel(df_filtered[EXPORT_COLUMNS])
            st.download_button("📥 Pobierz Excel", excel_all,
                file_name="baza_leadow.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, type="primary")
        with bc2:
            csv_data = df_filtered[EXPORT_COLUMNS].to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
            st.download_button("📥 Pobierz CSV", csv_data,
                file_name="baza_leadow.csv", mime="text/csv", use_container_width=True)
        with bc3:
            if st.button("🗑️ Wyczyść całą bazę", use_container_width=True):
                os.remove(MASTER_FILE)
                if GCS_BUCKET:
                    try:
                        _gcs_client().bucket(GCS_BUCKET).blob(MASTER_FILE).delete()
                    except Exception:
                        pass
                st.rerun()


# ── TAB 3: Historia zapytań ───────────────────────────────────────────────────
with tab_historia:
    history = load_history()

    if not history:
        st.info("Brak historii. Uruchom scraper żeby zarejestrować pierwsze zapytania.")
    else:
        df_hist = pd.DataFrame([
            {"Branża": k.split("|")[0].title(),
             "Miasto": k.split("|")[1].title(),
             "Data": v["data"],
             "Leady zebrane": v["leady"]}
            for k, v in sorted(history.items(), key=lambda x: x[1]["data"], reverse=True)
        ])

        hc1, hc2 = st.columns(2)
        hc1.metric("Wykonanych zapytań", len(df_hist))
        hc2.metric("Leadów łącznie z historii", df_hist["Leady zebrane"].sum())

        st.divider()
        st.dataframe(df_hist, use_container_width=True, height=500, hide_index=True)

        if st.button("🗑️ Wyczyść historię zapytań", use_container_width=True):
            os.remove(HISTORY_FILE)
            if GCS_BUCKET:
                try:
                    _gcs_client().bucket(GCS_BUCKET).blob(HISTORY_FILE).delete()
                except Exception:
                    pass
            st.rerun()


# ── TAB 4: Test scoringu ──────────────────────────────────────────────────────
with tab_test_scoring:
    st.markdown("Sprawdź jak `score_website()` ocenia konkretną firmę, zanim zaufasz algorytmowi przy masowym scrapingu.")

    tc1, tc2, tc3 = st.columns([2, 1, 1])
    with tc1:
        test_url = st.text_input("Adres WWW (puste = symulacja braku strony)", key="test_scoring_url", placeholder="https://przyklad.pl")
    with tc2:
        test_rating = st.number_input("Ocena (0-5)", min_value=0.0, max_value=5.0, value=4.0, step=0.1, key="test_scoring_rating")
    with tc3:
        test_reviews = st.number_input("Liczba opinii", min_value=0, value=10, step=1, key="test_scoring_reviews")

    if st.button("▶️ Uruchom scoring", type="primary", key="run_test_scoring"):
        with st.spinner("Sprawdzam stronę..."):
            score, status, breakdown = score_website(test_url.strip(), test_rating, test_reviews)

        if score >= 70:
            color, emoji = "green", "🟢"
        elif score >= 40:
            color, emoji = "orange", "🟡"
        else:
            color, emoji = "gray", "⚪"

        st.markdown(f"## {emoji} :{color}[{score} / 100]")
        st.markdown(f"**Website status:** `{status}`")

        st.markdown("**Uzasadnienie punktacji:**")
        for line in format_breakdown(breakdown):
            st.markdown(f"- {line}")

        with st.expander("Surowy breakdown (JSON)", expanded=False):
            st.json(breakdown)


# ── TAB 5: Konfiguracja scoringu ──────────────────────────────────────────────
with tab_konfig_scoring:
    st.markdown("Dostosuj wagi punktowe algorytmu scoringu. Zmiany zapisane tutaj obowiązują od razu w kolejnych scrapingach oraz w zakładce **Test scoringu**.")

    current_weights = load_weights()

    st.subheader("Komponent A — obecność strony (max 40 pkt)")
    a1, a2 = st.columns(2)
    with a1:
        w_brak_strony = st.number_input("Brak strony WWW", min_value=0, max_value=40, value=int(current_weights["brak_strony"]), key="w_brak_strony")
    with a2:
        w_strona_nie_dziala = st.number_input("Strona nie odpowiada", min_value=0, max_value=40, value=int(current_weights["strona_nie_dziala"]), key="w_strona_nie_dziala")

    st.subheader("Komponent B — jakość działającej strony (max 25 pkt)")
    b1, b2 = st.columns(2)
    with b1:
        w_brak_viewport = st.number_input("Brak viewport (niemobilna)", min_value=0, max_value=25, value=int(current_weights["brak_viewport"]), key="w_brak_viewport")
        w_wolna_strona_pkt = st.number_input("Wolna strona (punkty)", min_value=0, max_value=25, value=int(current_weights["wolna_strona_pkt"]), key="w_wolna_strona_pkt")
    with b2:
        w_brak_ssl = st.number_input("Brak SSL (http://)", min_value=0, max_value=25, value=int(current_weights["brak_ssl"]), key="w_brak_ssl")
        w_uboga_tresc = st.number_input("Uboga treść / brak tytułu", min_value=0, max_value=25, value=int(current_weights["uboga_tresc"]), key="w_uboga_tresc")
    w_wolna_strona_prog_sek = st.number_input("Próg 'wolnej strony' (sekundy)", min_value=0.5, max_value=30.0, value=float(current_weights["wolna_strona_prog_sek"]), step=0.5, key="w_wolna_strona_prog_sek")

    st.subheader("Komponent C — potencjał wg opinii Google (max 35 pkt)")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Liczba opinii**")
        w_opinie_50_plus = st.number_input("≥ 50 opinii", min_value=0, max_value=35, value=int(current_weights["opinie_50_plus"]), key="w_opinie_50_plus")
        w_opinie_15_49 = st.number_input("≥ 15 opinii", min_value=0, max_value=35, value=int(current_weights["opinie_15_49"]), key="w_opinie_15_49")
        w_opinie_1_14 = st.number_input("≥ 1 opinia", min_value=0, max_value=35, value=int(current_weights["opinie_1_14"]), key="w_opinie_1_14")
    with c2:
        st.markdown("**Ocena**")
        w_ocena_4_5_plus = st.number_input("≥ 4.5", min_value=0, max_value=35, value=int(current_weights["ocena_4_5_plus"]), key="w_ocena_4_5_plus")
        w_ocena_4_0_4_5 = st.number_input("≥ 4.0", min_value=0, max_value=35, value=int(current_weights["ocena_4_0_4_5"]), key="w_ocena_4_0_4_5")
        w_ocena_3_0_4_0 = st.number_input("≥ 3.0", min_value=0, max_value=35, value=int(current_weights["ocena_3_0_4_0"]), key="w_ocena_3_0_4_0")

    live_weights = {
        "brak_strony": w_brak_strony,
        "strona_nie_dziala": w_strona_nie_dziala,
        "brak_viewport": w_brak_viewport,
        "brak_ssl": w_brak_ssl,
        "wolna_strona_pkt": w_wolna_strona_pkt,
        "wolna_strona_prog_sek": w_wolna_strona_prog_sek,
        "uboga_tresc": w_uboga_tresc,
        "opinie_50_plus": w_opinie_50_plus,
        "opinie_15_49": w_opinie_15_49,
        "opinie_1_14": w_opinie_1_14,
        "ocena_4_5_plus": w_ocena_4_5_plus,
        "ocena_4_0_4_5": w_ocena_4_0_4_5,
        "ocena_3_0_4_0": w_ocena_3_0_4_0,
    }

    st.divider()
    bcol1, bcol2 = st.columns(2)
    with bcol1:
        if st.button("💾 Zapisz ustawienia", type="primary", use_container_width=True):
            save_weights(live_weights)
            st.success("✅ Nowe wagi zostały zapisane i są teraz aktywne — będą stosowane we wszystkich kolejnych scrapingach oraz w zakładce 'Test scoringu'.")
    with bcol2:
        if st.button("↩️ Przywróć domyślne", use_container_width=True):
            save_weights(DEFAULT_WEIGHTS)
            st.success("✅ Przywrócono domyślne wagi.")
            st.rerun()

    st.divider()
    st.subheader("🔎 Podgląd na żywo")
    st.caption("Przelicza się natychmiast po zmianie suwaków powyżej, przed zapisaniem.")

    pc1, pc2, pc3 = st.columns([2, 1, 1])
    with pc1:
        preview_url = st.text_input("Adres WWW (puste = brak strony)", key="konfig_preview_url", placeholder="https://przyklad.pl")
    with pc2:
        preview_rating = st.number_input("Ocena (0-5)", min_value=0.0, max_value=5.0, value=4.0, step=0.1, key="konfig_preview_rating")
    with pc3:
        preview_reviews = st.number_input("Liczba opinii", min_value=0, value=10, step=1, key="konfig_preview_reviews")

    preview_score, preview_status, preview_breakdown = score_website(
        preview_url.strip(), preview_rating, preview_reviews, weights=live_weights
    )

    if preview_score >= 70:
        p_color, p_emoji = "green", "🟢"
    elif preview_score >= 40:
        p_color, p_emoji = "orange", "🟡"
    else:
        p_color, p_emoji = "gray", "⚪"

    st.markdown(f"### {p_emoji} :{p_color}[{preview_score} / 100] — status: `{preview_status}`")
    for line in format_breakdown(preview_breakdown):
        st.markdown(f"- {line}")
    with st.expander("Surowy breakdown (JSON)", expanded=False):
        st.json(preview_breakdown)
