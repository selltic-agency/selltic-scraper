# Deploy Selltic Scraper na Google Cloud Run + subdomena + hasło

## 0. Co się zmieniło w kodzie względem oryginału
- Dodano bramkę hasłem na starcie aplikacji (zmienna środowiskowa `APP_PASSWORD`).
- Dodano opcjonalną synchronizację `baza_leadow.csv` i `historia_zapytan.json` z bucketem
  Google Cloud Storage (zmienna `GCS_BUCKET`) — **to jest wymagane**, bo Cloud Run nie ma
  trwałego dysku. Bez tego baza leadów zniknie przy pierwszym restarcie kontenera.

Pliki: `selltic_scraper.py`, `Dockerfile`, `requirements.txt` — wrzuć je do roota repo na
GitHubie (albo do podfolderu, wtedy trzeba ustawić "build context" w Cloud Run na ten folder).

---

## 1. Przygotowanie projektu GCP
```bash
gcloud config set project TWOJ_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
    artifactregistry.googleapis.com storage.googleapis.com
```

## 2. Bucket na trwałe dane
```bash
gcloud storage buckets create gs://selltic-scraper-data --location=europe-central2
```
(nazwa musi być globalnie unikalna — dostosuj)

## 3. Deploy z GitHuba (Continuous Deployment)
1. Wejdź w **Cloud Run** → **Create Service**.
2. Wybierz **„Continuously deploy from a repository"** → połącz konto GitHub →
   wskaż repozytorium i branch (np. `main`).
3. Build type: **Dockerfile** (Cloud Build sam go znajdzie w repo).
4. Region: `europe-central2` (Warszawa) lub `europe-west1` (Belgia) — bliżej PL.
5. Authentication: **„Allow unauthenticated invocations"** (logowanie robi Twoja bramka
   hasłem w appce, nie IAM — chyba że wolisz zabezpieczenie na poziomie Google, patrz pkt. 6).
6. **Minimum instances: 1, Maximum instances: 1.**
   To ważne — Twoja baza leadów to jeden plik CSV. Przy >1 instancji każda miałaby
   swoją kopię i dane by się rozjeżdżały. Przy min=1 unikasz też "zimnego startu".
7. W sekcji **Variables & Secrets** dodaj zmienne środowiskowe:
   - `APP_PASSWORD` = twoje-haslo
   - `GCS_BUCKET` = selltic-scraper-data
   - `GOOGLE_PLACES_API_KEY` = Twój klucz Google Places API (eliminuje wklejanie go ręcznie przy każdej sesji)
   - `CRM_API_BASE_URL` = adres Twojego CRM, np. `https://crm.selltic-agency.pl` (bez ukośnika na końcu)
   - `SCRAPER_IMPORT_KEY` = długi losowy string — **musi być identyczny** jak `SCRAPER_IMPORT_KEY`/`X-API-Key` ustawiony po stronie CRM
   - `EMBED_TOKEN` = długi losowy string — opcjonalny; jeśli ustawiony, otwarcie appki z `?token=<EMBED_TOKEN>` w URL (np. z iframe/reverse proxy w CRM) pomija ekran logowania, bo CRM już uwierzytelnił użytkownika
8. W **Security → Service account** upewnij się, że konto usługi ma rolę
   `Storage Object Admin` na utworzonym buckecie (żeby mógł zapisywać/czytać CSV).
9. Kliknij **Create** — Cloud Build zbuduje obraz z Dockerfile i wdroży na Cloud Run.

Od teraz każdy push do brancha `main` automatycznie zrobi nowy deploy.

## 4. Podpięcie subdomeny scraper.selltic-agency.pl
1. W Cloud Run wejdź w **Manage Custom Domains** → **Add Mapping**.
2. Wybierz wdrożony serwis, wpisz `scraper.selltic-agency.pl`.
3. Google pokaże Ci rekord DNS do dodania (zwykle **CNAME** wskazujący na `ghs.googlehosted.com`,
   czasem zestaw rekordów A/AAAA jeśli mapujesz domenę bez www).
4. Wejdź w panel DNS Twojego rejestratora domeny `selltic-agency.pl` i dodaj ten rekord
   dla subdomeny `scraper`.
5. Poczekaj na propagację DNS (od kilku minut do kilku godzin) — Google sam wystawi
   certyfikat SSL (Google-managed certificate), nie musisz nic robić ręcznie.

Uwaga: domain mapping na Cloud Run działa tylko w niektórych regionach (m.in.
`europe-west1`) — jeśli wybrałeś `europe-central2` i mapowanie nie zadziała, przełóż
serwis na `europe-west1`.

## 5. Test
- Otwórz `https://scraper.selltic-agency.pl` → powinieneś zobaczyć ekran z polem na hasło.
- Po wpisaniu poprawnego hasła z `APP_PASSWORD` → wchodzisz do scrapera.
- Zrób testowe zapytanie, sprawdź w **Cloud Storage → bucket → selltic-scraper-data**
  czy pojawił się tam `baza_leadow.csv`.

---

## 6. NOWE: headless webhook backend (architektura bez Streamlit UI)

Obok istniejącej appki Streamlit (niezmienionej, dalej działa jak dotychczas)
dochodzi **druga, osobna usługa Cloud Run**: `webhook_server.py` (FastAPI).
CRM (selltic-crm) staje się jedynym interfejsem użytkownika — nowa zakładka
"Scraper" w CRM tworzy zadania w Supabase i woła webhook poniżej, który robi
scraping i zapisuje wyniki bezpośrednio do Supabase (bez pośredniego API).

**Nic w starym flow nie zostało usunięte** — stary endpoint `/api/prospecting/import`
po stronie CRM i stara appka Streamlit nadal działają równolegle, do potwierdzenia
że nowy flow działa end-to-end.

### 6.1 Nowe pliki
- `scraper_core.py` — logika scrapowania/scoringu bez Streamlit (współdzielona)
- `supabase_backend.py` — klient Supabase + wczytywanie `scraper_config`
- `webhook_server.py` — FastAPI, endpoint `POST /webhook/scrape` + `GET /health-check`
- `Dockerfile.webhook`, `cloudbuild.webhook.yaml` — build/deploy tej usługi

### 6.2 Deploy jako osobny serwis Cloud Run
1. **Cloud Run** → **Create Service** → **Continuously deploy from a repository**,
   ten sam repo/branch co stara appka.
2. Build type: **Dockerfile**, ale wskaż `Dockerfile.webhook` jako plik Dockerfile
   (w ustawieniach source repo Cloud Build) — **NIE** `Dockerfile` (to stara appka).
3. Nazwa serwisu np. `selltic-scraper-webhook` (musi być inna niż istniejący serwis).
4. **CPU allocation: "CPU is always allocated"** (`--no-cpu-throttling`) —
   **wymagane**, bo webhook odpowiada 202 natychmiast i kontynuuje scraping w tle
   (FastAPI BackgroundTasks) już po wysłaniu odpowiedzi. Bez tego Cloud Run może
   zamrozić CPU zaraz po odpowiedzi i przetwarzanie w tle się nie dokończy.
5. Authentication: **"Allow unauthenticated invocations"** — autoryzacja jest
   na poziomie aplikacji (nagłówek `Authorization: Bearer <SCRAPER_WEBHOOK_SECRET>`),
   tak samo jak stary `SCRAPER_IMPORT_KEY`.
6. Zmienne środowiskowe (**Variables & Secrets**):
   - `SUPABASE_URL` — **musi być identyczne** jak `NEXT_PUBLIC_SUPABASE_URL` w CRM (Vercel)
   - `SUPABASE_SERVICE_ROLE_KEY` — **musi być identyczne** jak `SUPABASE_SERVICE_ROLE_KEY` w CRM (Vercel)
   - `SCRAPER_WEBHOOK_SECRET` — długi losowy string, **musi być identyczny** jak
     `SCRAPER_WEBHOOK_SECRET` w CRM (Vercel)
   - `GCS_BUCKET` — ten sam bucket co stara appka (kopia zapasowa CSV per zadanie)
   - `GOOGLE_PLACES_API_KEY` — opcjonalny fallback, jeśli `scraper_config.google_places_api_key`
     w Supabase jest puste
7. Service account: ta sama rola `Storage Object Admin` na buckecie co stara appka.

### 6.3 Uwaga o requirements.txt
`requirements.txt` jest współdzielony między obiema usługami (Streamlit +
FastAPI/Supabase razem) dla prostoty — każdy obraz instaluje trochę
niepotrzebnych zależności drugiej usługi. Jeśli to problem (rozmiar obrazu,
czas builda), można to później rozdzielić na `requirements-streamlit.txt` /
`requirements-webhook.txt`.

---

## Dodatkowe uwagi
- **Bezpieczeństwo hasła**: to jest bramka na poziomie aplikacji (porównanie stringów),
  wystarczająca żeby przypadkowi ludzie nie weszli na scraper, ale to nie jest pełne
  uwierzytelnianie. Jeśli zależy Ci na czymś mocniejszym, Cloud Run wspiera
  **Identity-Aware Proxy (IAP)** — logowanie przez konto Google — ale to bardziej
  złożona konfiguracja i nie pasuje do modelu "proste hasło".
- **Koszt**: Cloud Run z min instances=1 oznacza, że jedna instancja działa 24/7 —
  to nie jest "scale to zero", więc policzy Ci się ciągły, niewielki koszt
  (przy małym kontenerze rzędu kilku-kilkunastu zł/mc). Jeśli wolisz zero kosztu
  w przestojach kosztem dłuższego pierwszego ładowania, możesz dać min instances=0,
  ale wtedy musisz pilnować, żeby nikt nie odpalił dwóch sesji jednocześnie
  (ryzyko dwóch instancji nadpisujących sobie CSV).
- Klucz Google Places API nadal wpisujesz ręcznie w sidebarze appki przy każdej sesji
  (tak jak w oryginalnym skrypcie) — jeśli chcesz, mogę to też przepiąć na zmienną
  środowiskową, żeby nie trzeba było go wklejać za każdym razem.
