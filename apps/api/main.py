"""uvicorn entrypoint for the Bill Commons API.

Local dev:      .venv/bin/uvicorn main:app --app-dir apps/api --reload --port 8000
Production:     uvicorn main:app --app-dir apps/api --host 0.0.0.0 --port $PORT
(Procfile-style: `web: uvicorn main:app --app-dir apps/api --host 0.0.0.0 --port $PORT`)
"""
from billcommons_api.app import create_app

app = create_app()
