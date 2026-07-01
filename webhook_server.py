"""
webhook_server.py — headless backend na Cloud Run, wołany webhookiem z CRM.

Endpoint: POST /webhook/scrape
Auth:     Authorization: Bearer <SCRAPER_WEBHOOK_SECRET>
Body:     {"batch_id": "uuid", "job_ids": ["uuid", ...]}

Odpowiada 202 natychmiast (po sprawdzeniu istnienia zadań), a właściwe
scrapowanie odbywa się w tle (FastAPI BackgroundTasks) — CRM śledzi postęp
przez Supabase Realtime na tabeli scrape_jobs, nie przez odpowiedź HTTP.

WAŻNE (Cloud Run): wdróż tę usługę z "CPU always allocated" (lub min-instances
>= 1), inaczej Cloud Run może stopować CPU zaraz po wysłaniu odpowiedzi 202
i przetwarzanie w tle się nie dokończy.
"""
import csv
import io
import math
import os
import time
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel

from scraper_core import gcs_upload_bytes, place_details, score_website, text_search
from supabase_backend import (
    get_owner_id,
    get_supabase,
    load_scraper_config,
    resolve_api_key,
    scoring_weights_from_config,
)

app = FastAPI(title="Selltic Scraper — headless backend")

SCRAPER_WEBHOOK_SECRET = os.environ.get("SCRAPER_WEBHOOK_SECRET", "")

LEAD_COLUMNS = [
    "place_id", "business_name", "phone", "address", "website", "rating",
    "review_count", "business_status", "score", "website_status",
    "source_keyword", "source_location", "scraped_at",
]


class ScrapeWebhookRequest(BaseModel):
    batch_id: str
    job_ids: list[str]


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/webhook/scrape")
def webhook_scrape(
    payload: ScrapeWebhookRequest,
    background_tasks: BackgroundTasks,
    authorization: str = Header(default=""),
):
    if not SCRAPER_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Endpoint nieskonfigurowany (brak SCRAPER_WEBHOOK_SECRET)")
    if authorization != f"Bearer {SCRAPER_WEBHOOK_SECRET}":
        raise HTTPException(status_code=401, detail="Brak autoryzacji")

    if not payload.job_ids:
        raise HTTPException(status_code=400, detail="job_ids nie może być puste")

    db = get_supabase()
    res = db.table("scrape_jobs").select("id,status").in_("id", payload.job_ids).execute()
    found = {row["id"]: row["status"] for row in (res.data or [])}

    accepted, rejected = [], []
    for job_id in payload.job_ids:
        status = found.get(job_id)
        if status is None:
            rejected.append({"job_id": job_id, "reason": "not found"})
        elif status == "running":
            rejected.append({"job_id": job_id, "reason": "already running"})
        else:
            accepted.append(job_id)

    if accepted:
        background_tasks.add_task(process_batch, accepted)

    return {"accepted": accepted, "rejected": rejected}


def process_batch(job_ids: list[str]):
    db = get_supabase()
    owner_id = get_owner_id()
    cfg = load_scraper_config(owner_id)
    api_key = resolve_api_key(cfg)
    weights = scoring_weights_from_config(cfg)
    max_results = int(cfg.get("max_results_per_query") or 60)
    delay_s = float(cfg.get("request_delay_ms") or 500) / 1000.0
    max_strony = min(3, math.ceil(max_results / 20))

    for job_id in job_ids:
        try:
            process_job(db, job_id, api_key, weights, max_strony, delay_s, owner_id)
        except Exception as e:
            db.table("scrape_jobs").update({
                "status": "error",
                "error_message": str(e)[:2000],
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", job_id).execute()


def process_job(db, job_id: str, api_key: str, weights: dict, max_strony: int, delay_s: float, owner_id: str):
    job_res = db.table("scrape_jobs").select("*").eq("id", job_id).single().execute()
    job = job_res.data
    if not job:
        return

    db.table("scrape_jobs").update({
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()

    if not api_key:
        raise RuntimeError("Brak klucza Google Places API (scraper_config.google_places_api_key)")

    keyword = job["keyword"]
    location = job["location"]
    query = f"{keyword} {location}"

    rows = []
    next_token = None
    for page_nr in range(max_strony):
        if page_nr > 0 and next_token:
            time.sleep(2)  # token Google Places staje się aktywny z opóźnieniem

        data = text_search(query, api_key, next_token if page_nr > 0 else None)
        api_status = data.get("status")
        if api_status == "REQUEST_DENIED":
            raise RuntimeError(f"REQUEST_DENIED — sprawdź klucz API i rozliczenia: {data.get('error_message', '')}")
        if api_status in ("ZERO_RESULTS",):
            break
        if api_status != "OK":
            raise RuntimeError(f"Google Places API status={api_status}: {data.get('error_message', '')}")

        for place in data.get("results", []):
            pid = place["place_id"]
            details = place_details(pid, api_key)
            time.sleep(delay_s)

            website = details.get("website", "") or None
            rating = details.get("rating")
            review_count = details.get("user_ratings_total")
            score, website_status, breakdown = score_website(website, rating, review_count, weights)

            rows.append({
                "owner": owner_id,
                "job_id": job_id,
                "place_id": pid,
                "business_name": details.get("name", ""),
                "phone": details.get("formatted_phone_number") or None,
                "address": details.get("formatted_address") or None,
                "website": website,
                "rating": rating,
                "review_count": review_count,
                "business_status": details.get("business_status") or None,
                "score": score,
                "score_breakdown": breakdown,
                "website_status": website_status,
                "source_keyword": keyword,
                "source_location": location,
                "status": "new",
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            })

        next_token = data.get("next_page_token")
        if not next_token:
            break

    if rows:
        db.table("scraped_leads").upsert(rows, on_conflict="owner,place_id").execute()
        _write_gcs_backup(job_id, rows)

    db.table("scrape_jobs").update({
        "status": "done",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "results_count": len(rows),
    }).eq("id", job_id).execute()


def _write_gcs_backup(job_id: str, rows: list[dict]):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LEAD_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    gcs_upload_bytes(f"scrape_jobs/{job_id}.csv", buf.getvalue().encode("utf-8-sig"), content_type="text/csv")
