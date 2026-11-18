"""Download timetable tabs as CSV from the published Google Sheet."""
from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx


class SheetFetchError(RuntimeError):
    pass


def source_label(url: str) -> str:
    """Stable short label for a tab, e.g. 'gid:1454027058'."""
    gid = parse_qs(urlparse(url).query).get("gid", [""])[0]
    return f"gid:{gid}" if gid else urlparse(url).path[-24:]


async def fetch_csv(url: str, client: httpx.AsyncClient | None = None, attempts: int = 3) -> str:
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=True)
    try:
        last = "unknown"
        for attempt in range(attempts):
            try:
                response = await client.get(url, headers={"Cache-Control": "no-cache"})
                if response.status_code == 200:
                    ctype = response.headers.get("content-type", "")
                    text = response.content.decode("utf-8-sig", errors="replace")
                    # A private sheet answers 200 with an HTML sign-in page, not CSV.
                    if "text/html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
                        raise SheetFetchError("Jadval CSV o'rniga HTML qaytardi (ruxsat yopilgan bo'lishi mumkin)")
                    return text
                last = f"HTTP {response.status_code}"
                if response.status_code in (401, 403, 404):
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(1.5 * (attempt + 1))
        raise SheetFetchError(f"Jadvalni yuklab bo'lmadi: {last}")
    finally:
        if own:
            await client.aclose()
