"""Minimal read-only client for the Zotero Web API v3 (https://www.zotero.org/support/dev/web_api/v3).

Used endpoints (all relative to /groups/<id> or /users/<id>):
  GET /items?since=<v>&includeTrashed=1&start=<n>&limit=100   headers: Total-Results, Last-Modified-Version
  GET /collections?start=<n>&limit=100
  GET /deleted?since=<v>                                        {"items": [...], "collections": [...], ...}
  GET /items/<key>/file                                         302 -> file on Zotero's storage (S3)

The API key goes in the `Zotero-API-Key` header only. Redirects are followed by hand so that
the key is never sent to the storage host, and it never appears in logs or error messages.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterator

from .settings import ZoteroError

log = logging.getLogger(__name__)

USER_AGENT = "rag-drg/0.1 (research group literature sync)"
MAX_RETRIES = 4
MAX_WAIT = 120.0


class FileNotAvailable(ZoteroError):
    """The attachment has no stored file (e.g. never synced to Zotero storage)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # surface 3xx as HTTPError; we follow it ourselves without the key


class ZoteroWebAPI:
    def __init__(self, api_base: str, library_prefix: str, api_key: str | None, *,
                 timeout: float = 60.0, page_size: int = 100, sleep: Callable[[float], None] = time.sleep):
        self.base = f"{api_base.rstrip('/')}/{library_prefix}"
        self._key = api_key
        self.timeout = timeout
        self.page_size = page_size
        self.sleep = sleep
        self._opener = urllib.request.build_opener(_NoRedirect)
        self.requests = 0

    def __repr__(self) -> str:  # never show the key
        return f"ZoteroWebAPI({self.base!r})"

    # ------------------------------------------------------------------ #
    def _open(self, url: str, with_key: bool) -> tuple[int, dict, bytes]:
        headers = {"User-Agent": USER_AGENT}
        if with_key:
            headers["Zotero-API-Version"] = "3"
            if self._key:
                headers["Zotero-API-Key"] = self._key
        for attempt in range(MAX_RETRIES + 1):
            self.requests += 1
            req = urllib.request.Request(url, headers=headers)
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    status, hdrs, body = resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
            except urllib.error.HTTPError as e:
                status, hdrs = e.code, {k.lower(): v for k, v in (e.headers or {}).items()}
                body = e.read() if e.fp else b""
            except urllib.error.URLError as e:
                if attempt < MAX_RETRIES:
                    self.sleep(min(MAX_WAIT, 2.0 ** attempt))
                    continue
                raise ZoteroError(f"cannot reach {_safe(url)}: {e.reason}") from None
            if status in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                wait = _seconds(hdrs.get("retry-after")) or min(MAX_WAIT, 2.0 ** (attempt + 1))
                log.info("Zotero API %s, retrying in %.0fs", status, wait)
                self.sleep(min(MAX_WAIT, wait))
                continue
            backoff = _seconds(hdrs.get("backoff"))
            if backoff:  # server asks clients to slow down; honour it before the next request
                self.sleep(min(MAX_WAIT, backoff))
            return status, hdrs, body
        raise ZoteroError(f"giving up on {_safe(url)} after {MAX_RETRIES} retries")

    def _get(self, path: str, params: dict | None = None) -> tuple[dict, bytes]:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, hdrs, body = self._open(url, with_key=True)
        if status == 200:
            return hdrs, body
        if status == 403:
            raise ZoteroError(f"403 Forbidden for {_safe(url)}: the API key has no read access to this "
                              "library (check the key's group permissions) or the library ID is wrong")
        if status == 404:
            raise ZoteroError(f"404 Not Found for {_safe(url)}: check library_type/library_id")
        raise ZoteroError(f"HTTP {status} for {_safe(url)}: {body[:200].decode(errors='replace')}")

    def get_json(self, path: str, params: dict | None = None) -> tuple[dict, object]:
        hdrs, body = self._get(path, params)
        return hdrs, json.loads(body.decode("utf-8") or "null")

    def pages(self, path: str, params: dict | None = None) -> Iterator[tuple[dict, list]]:
        """Yield (headers, page) using start/limit until Total-Results is reached."""
        start = 0
        while True:
            hdrs, page = self.get_json(path, {**(params or {}), "start": start, "limit": self.page_size})
            page = page or []
            yield hdrs, page
            start += len(page)
            total = int(hdrs.get("total-results", start) or start)
            if not page or start >= total:
                return

    # ------------------------------------------------------------------ #
    def collections(self) -> list[dict]:
        out: list[dict] = []
        for _, page in self.pages("/collections"):
            out.extend(page)
        return out

    def items_since(self, since: int) -> tuple[int, list[dict]]:
        """All items (incl. children and trashed ones) modified after `since`, plus the library
        version they belong to. Restarts if the library changes while paging."""
        for _ in range(3):
            items: list[dict] = []
            version: int | None = None
            restarted = False
            for hdrs, page in self.pages("/items", {"since": since, "includeTrashed": 1, "format": "json"}):
                v = int(hdrs.get("last-modified-version") or 0)
                if version is None:
                    version = v
                elif v != version:
                    log.info("Library changed during sync (%s -> %s); restarting", version, v)
                    restarted = True
                    break
                items.extend(page)
            if not restarted:
                return int(version or since), items
        raise ZoteroError("library kept changing during sync; try again later")

    def deleted_since(self, since: int) -> dict:
        _, data = self.get_json("/deleted", {"since": since})
        return data if isinstance(data, dict) else {}

    def download(self, key: str, max_bytes: int) -> bytes:
        url = f"{self.base}/items/{urllib.parse.quote(key)}/file"
        status, hdrs, body = self._open(url, with_key=True)
        hops = 0
        while status in (301, 302, 303, 307, 308) and hops < 5:
            loc = hdrs.get("location")
            if not loc:
                break
            url = urllib.parse.urljoin(url, loc)
            # Only send the key back to the API host itself, never to the storage host.
            same_host = urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(self.base).netloc
            status, hdrs, body = self._open(url, with_key=same_host)
            hops += 1
        if status == 404:
            raise FileNotAvailable("no stored file (404)")
        if status != 200:
            raise ZoteroError(f"HTTP {status} downloading file")
        if len(body) > max_bytes:
            raise ZoteroError(f"file larger than {max_bytes // 1_000_000} MB, skipped")
        return body


def _seconds(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _safe(url: str) -> str:
    """URL for messages: drop the query string (never holds the key, but keep messages short)."""
    return url.split("?", 1)[0]
