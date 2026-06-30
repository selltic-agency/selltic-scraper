FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY selltic_scraper.py .

# Cloud Run wstrzykuje port w zmiennej $PORT - domyślnie 8080
ENV PORT=8080
EXPOSE 8080

CMD streamlit run selltic_scraper.py \
    --server.port=${PORT} \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
