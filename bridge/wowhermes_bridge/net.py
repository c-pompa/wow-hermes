"""Minimal HTTP client for the two downloads the bridge is allowed to make.

Two jobs, both security-critical:

* **Redirects are re-validated on every hop.** ``http.client`` follows nothing, so every
  ``Location`` passes through the caller's allowlist check before it is opened. A 302 to
  ``http://evil.example/x.exe`` therefore dies at the check, not at the payload.
* **Size is capped while reading**, not from ``Content-Length`` (a lying header must not
  buy an unbounded write to disk).

``overrides`` maps an allowlisted hostname to ``(host, port)``. It exists **only** so the
tests can point ``edge.forgecdn.net`` at a local fixture server; the URL allowlist is still
applied to the original URL, so an override can never widen what is reachable.
"""

from __future__ import annotations

import hashlib
import http.client
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

__all__ = ["BlockedURL", "FetchError", "Fetcher", "Response", "DownloadResult", "safe_url"]

CHUNK = 64 * 1024
USER_AGENT = "wowhermes-bridge/1.1 (local addon installer)"


class BlockedURL(ValueError):
    """The URL (or a redirect target) is outside the caller's allowlist."""

    def __init__(self, reason: str, url: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.url = url


class FetchError(RuntimeError):
    """Network/protocol failure, with a machine-readable reason."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class DownloadResult:
    url: str
    path: Path
    size: int
    sha256: str


def safe_url(url: str) -> str:
    """A loggable form of a URL: scheme, host and path only — never the query string.

    The CurseForge CDN carries ``?api-key=`` in some URLs, and signed download URLs carry
    tokens. Nothing in the bridge may log those.
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return "(unparseable url)"
    host = parts.hostname or ""
    return f"{parts.scheme}://{host}{parts.path}"


class Fetcher:
    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_redirects: int = 5,
        user_agent: str = USER_AGENT,
        overrides: dict[str, tuple[str, int]] | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.user_agent = user_agent
        self.overrides = {k.lower(): v for k, v in (overrides or {}).items()}
        self._ssl = ssl_context or ssl.create_default_context()

    # -- low level ---------------------------------------------------------
    def _connection(self, url: str) -> tuple[http.client.HTTPConnection, str, dict[str, str]]:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if not host:
            raise FetchError("missing_host", safe_url(url))
        override = self.overrides.get(host.lower())
        if override is not None:
            conn: http.client.HTTPConnection = http.client.HTTPConnection(
                override[0], override[1], timeout=self.timeout
            )
            host_header = host if parts.port in (None, 80, 443) else f"{host}:{parts.port}"
            return conn, host_header, {"Host": host_header}
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(host, parts.port or 443, timeout=self.timeout, context=self._ssl)
        elif parts.scheme == "http":
            conn = http.client.HTTPConnection(host, parts.port or 80, timeout=self.timeout)
        else:
            raise FetchError("scheme_not_allowed", safe_url(url))
        return conn, host, {}

    @staticmethod
    def _target(url: str) -> str:
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        return target

    def _request(self, url: str, headers: dict[str, str]) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        conn, _host, extra = self._connection(url)
        merged = {"User-Agent": self.user_agent, "Accept": "*/*", "Accept-Encoding": "identity"}
        merged.update(extra)
        merged.update(headers)
        try:
            conn.request("GET", self._target(url), headers=merged)
            return conn, conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise FetchError("network_error", f"{type(exc).__name__}: {exc}") from exc

    def _open(
        self,
        url: str,
        *,
        check: Callable[[str], None],
        headers: dict[str, str] | None = None,
    ) -> tuple[str, http.client.HTTPConnection, http.client.HTTPResponse]:
        """Follow redirects (each hop re-checked) and return the final hop."""
        current = url
        headers = headers or {}
        for _hop in range(self.max_redirects + 1):
            check(current)
            conn, response = self._request(current, headers)
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                response.read()
                conn.close()
                if not location:
                    raise FetchError("bad_redirect", safe_url(current))
                current = urljoin(current, location)
                continue
            return current, conn, response
        raise FetchError("too_many_redirects", safe_url(url))

    # -- public ------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        check: Callable[[str], None],
        headers: dict[str, str] | None = None,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> Response:
        final, conn, response = self._open(url, check=check, headers=headers)
        try:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError("too_large", f"{safe_url(final)} over {max_bytes} B")
                chunks.append(chunk)
            return Response(
                url=final,
                status=response.status,
                headers={k.lower(): v for k, v in response.getheaders()},
                body=b"".join(chunks),
            )
        finally:
            conn.close()

    def download(
        self,
        url: str,
        path: Path,
        *,
        check: Callable[[str], None],
        headers: dict[str, str] | None = None,
        max_bytes: int = 32 * 1024 * 1024,
    ) -> DownloadResult:
        final, conn, response = self._open(url, check=check, headers=headers)
        if response.status != 200:
            response.read()
            conn.close()
            raise FetchError("http_error", f"HTTP {response.status} for {safe_url(final)}")
        digest = hashlib.sha256()
        total = 0
        try:
            with open(path, "wb") as fh:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError("too_large", f"{safe_url(final)} over {max_bytes} B")
                    digest.update(chunk)
                    fh.write(chunk)
            if total == 0:
                raise FetchError("empty_download", safe_url(final))
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        finally:
            conn.close()
        return DownloadResult(url=final, path=path, size=total, sha256=digest.hexdigest())
