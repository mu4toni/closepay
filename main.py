"""Local development ASGI server entrypoint.

In production on Vercel, requests are served directly by ``api/index.py``
using Serverless Functions and Vercel Cron (without long-polling or APScheduler).
"""

from __future__ import annotations

import uvicorn

from api.index import app

__all__ = ["app"]


if __name__ == "__main__":
    uvicorn.run(
        "api.index:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
