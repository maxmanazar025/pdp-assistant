"""Async Google Calendar v3 + Google Tasks v1 client (OAuth2 refresh-token flow).

A personal Gmail account cannot delegate Google Tasks to a service account, so the bot
holds a long-lived refresh token for ``maxmanazar025@gmail.com`` (obtained once with
``scripts/google_auth.py``) and mints short-lived access tokens on demand.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from memora import timeutil
from memora.config import Settings

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
CAL_BASE = "https://www.googleapis.com/calendar/v3"
TASKS_BASE = "https://tasks.googleapis.com/tasks/v1"
SCOPES = ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/tasks"]


class GoogleError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GoogleAuthError(GoogleError):
    """Refresh token missing, revoked or expired — needs human action."""


class GoogleClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._client = client or httpx.AsyncClient(timeout=20)
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._s.google_enabled

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ auth / transport
    async def _access_token(self) -> str:
        if not self.enabled:
            raise GoogleAuthError("Google hisobi ulanmagan (GOOGLE_REFRESH_TOKEN yo'q)")
        async with self._lock:
            if self._token and time.monotonic() < self._token_exp - 60:
                return self._token
            response = await self._client.post(TOKEN_URL, data={
                "client_id": self._s.google_client_id,
                "client_secret": self._s.google_client_secret,
                "refresh_token": self._s.google_refresh_token,
                "grant_type": "refresh_token",
            })
            if response.status_code in (400, 401):
                raise GoogleAuthError(f"Google refresh token rad etildi: {response.text[:200]}", response.status_code)
            response.raise_for_status()
            data = response.json()
            self._token = data["access_token"]
            self._token_exp = time.monotonic() + float(data.get("expires_in", 3600))
            return self._token

    async def _request(self, method: str, url: str, *, params: dict | None = None, json: dict | None = None) -> Any:
        last: Exception | None = None
        for attempt in range(4):
            token = await self._access_token()
            try:
                response = await self._client.request(
                    method, url, params=params, json=json, headers={"Authorization": f"Bearer {token}"}
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = GoogleError(f"{type(exc).__name__}: {exc}")
            else:
                if response.status_code == 401 and attempt == 0:
                    self._token = None  # stale access token: mint a new one and retry
                    continue
                if response.status_code in (429, 500, 502, 503, 504) or (
                    response.status_code == 403 and "ateLimit" in response.text
                ):
                    last = GoogleError(f"HTTP {response.status_code}: {response.text[:200]}", response.status_code)
                elif response.status_code >= 400:
                    raise GoogleError(f"HTTP {response.status_code}: {response.text[:300]}", response.status_code)
                else:
                    return response.json() if response.content else None
            await asyncio.sleep(min(8.0, 0.5 * 2 ** attempt))
        assert last is not None
        raise last

    # ------------------------------------------------------------------ calendar
    @staticmethod
    def _event_time(dt: datetime, tzname: str) -> dict[str, str]:
        return {"dateTime": timeutil.iso(dt), "timeZone": tzname}

    async def list_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "timeMin": timeutil.iso(start), "timeMax": timeutil.iso(end),
            "singleEvents": "true", "orderBy": "startTime", "maxResults": 250,
        }
        while True:
            data = await self._request("GET", f"{CAL_BASE}/calendars/{self._s.google_calendar_id}/events", params=params)
            items.extend(e for e in data.get("items", []) if e.get("status") != "cancelled")
            if not data.get("nextPageToken"):
                return items
            params["pageToken"] = data["nextPageToken"]

    async def get_event(self, event_id: str) -> dict[str, Any]:
        return await self._request("GET", f"{CAL_BASE}/calendars/{self._s.google_calendar_id}/events/{event_id}")

    async def create_event(self, summary: str, start: datetime, end: datetime,
                           location: str | None = None, description: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": summary,
            "start": self._event_time(start, self._s.timezone),
            "end": self._event_time(end, self._s.timezone),
            "reminders": {"useDefault": True},
        }
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        return await self._request("POST", f"{CAL_BASE}/calendars/{self._s.google_calendar_id}/events", json=body)

    async def update_event(self, event_id: str, *, summary: str | None = None, start: datetime | None = None,
                           end: datetime | None = None, location: str | None = None,
                           description: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if summary is not None:
            body["summary"] = summary
        if start is not None:
            body["start"] = self._event_time(start, self._s.timezone)
        if end is not None:
            body["end"] = self._event_time(end, self._s.timezone)
        if location is not None:
            body["location"] = location
        if description is not None:
            body["description"] = description
        return await self._request(
            "PATCH", f"{CAL_BASE}/calendars/{self._s.google_calendar_id}/events/{event_id}", json=body
        )

    async def delete_event(self, event_id: str) -> None:
        try:
            await self._request("DELETE", f"{CAL_BASE}/calendars/{self._s.google_calendar_id}/events/{event_id}")
        except GoogleError as exc:
            if exc.status not in (404, 410):  # already gone is success
                raise

    # ------------------------------------------------------------------ tasks
    @staticmethod
    def due_field(day: date) -> str:
        """Google Tasks keeps only the *date* of ``due``; the time part is discarded by the API."""
        return f"{day.isoformat()}T00:00:00.000Z"

    async def list_tasks(self, updated_min: datetime | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "maxResults": 100, "showCompleted": "true", "showHidden": "true", "showDeleted": "true",
        }
        if updated_min is not None:
            params["updatedMin"] = updated_min.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        while True:
            data = await self._request("GET", f"{TASKS_BASE}/lists/{self._s.google_tasklist_id}/tasks", params=params)
            items.extend(data.get("items", []))
            if not data.get("nextPageToken"):
                return items
            params["pageToken"] = data["nextPageToken"]

    async def create_task(self, title: str, notes: str | None, due: date | None) -> dict[str, Any]:
        body: dict[str, Any] = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            body["due"] = self.due_field(due)
        return await self._request("POST", f"{TASKS_BASE}/lists/{self._s.google_tasklist_id}/tasks", json=body)

    async def patch_task(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "PATCH", f"{TASKS_BASE}/lists/{self._s.google_tasklist_id}/tasks/{task_id}", json=body
        )

    async def delete_task(self, task_id: str) -> None:
        try:
            await self._request("DELETE", f"{TASKS_BASE}/lists/{self._s.google_tasklist_id}/tasks/{task_id}")
        except GoogleError as exc:
            if exc.status not in (404, 410):
                raise


def parse_event_times(event: dict[str, Any]) -> tuple[datetime, datetime, bool]:
    """Return (start, end, all_day) for a Calendar API event resource."""
    start, end = event.get("start", {}), event.get("end", {})
    if "dateTime" in start:
        return (
            timeutil.parse_iso_local(start["dateTime"]),
            timeutil.parse_iso_local(end.get("dateTime", start["dateTime"])),
            False,
        )
    first = date.fromisoformat(start["date"])
    last = date.fromisoformat(end.get("date", start["date"]))
    begin = timeutil.combine(first, datetime.min.time())
    finish = timeutil.combine(last, datetime.min.time())
    return begin, max(finish, begin + timedelta(days=1)), True
