"""
Production WSGI entrypoint.

Local dev:   python app.py                  (Flask reloader, debug on)
Production:  APP_ENV=production python wsgi.py        (waitress, debug off)
         or: APP_ENV=production gunicorn wsgi:application --workers 1 --threads 8

A single worker is intentional: the Playwright scraper holds one shared headless
browser, and the in-memory caches/jobs live in-process.  Scale with threads, not
workers (or move the scraper/cache to a shared service first).
"""
import os

from app import app as application, _start_background_jobs

# Background price-refresh + PSA login pre-warm (the dev reloader does this in
# app.py; under a WSGI server we kick it off here, once).
_start_background_jobs(int(os.getenv("PORT", "5001")))


if __name__ == "__main__":
    from waitress import serve
    port = int(os.getenv("PORT", "5001"))
    print(f"PokePop running (production) on http://0.0.0.0:{port}")
    serve(application, host="0.0.0.0", port=port, threads=8)
