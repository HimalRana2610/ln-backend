"""Vercel serverless entrypoint.

Vercel's Python runtime looks for `api/index.py` and serves the ASGI callable
named `app`. Everything else lives in the `app` package, unchanged — the same
code runs under `uvicorn` locally.
"""

from app.main import app

__all__ = ["app"]
