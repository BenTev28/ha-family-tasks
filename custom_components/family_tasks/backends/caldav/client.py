"""Small async CalDAV client for VTODO collections.

Uses Home Assistant's aiohttp session directly instead of the ``caldav``
library: we need exact control over ETags (If-Match / If-None-Match),
sync-collection and raw iCalendar bodies, and it avoids any interaction with
the ``caldav`` version that the built-in CalDAV integration depends on.

Never log URLs, credentials or bodies above DEBUG; error messages carry only
HTTP status codes and collection *paths*.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import logging
from urllib.parse import unquote, urljoin, urlsplit
import xml.etree.ElementTree as ET

import aiohttp

_LOGGER = logging.getLogger(__name__)

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "cs": "http://calendarserver.org/ns/"}
TIMEOUT = aiohttp.ClientTimeout(total=30)


class CalDavError(Exception):
    """Base error (message is safe to log)."""


class CalDavAuthError(CalDavError):
    """401/403 on authentication."""


class CalDavConnectionError(CalDavError):
    """DNS / TCP / TLS / timeout problems."""


class PreconditionFailed(CalDavError):
    """412: the resource changed since we read it."""


class NotFound(CalDavError):
    """404."""


class SyncTokenInvalid(CalDavError):
    """The server no longer accepts our sync token."""


@dataclass
class Collection:
    """A calendar collection that supports VTODO."""

    path: str
    name: str
    color: str | None
    ctag: str | None
    sync_token: str | None


@dataclass
class Resource:
    """One calendar object resource."""

    href: str
    etag: str | None
    data: str | None


@dataclass
class SyncResult:
    """Outcome of a sync-collection REPORT."""

    token: str | None
    changed: list[str]  # hrefs
    deleted: list[str]  # hrefs


def _strip_etag(value: str | None) -> str | None:
    return value if value is None else value.strip()


class CalDavClient:
    """Async CalDAV client bound to one account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
    ) -> None:
        self._session = session
        self._base = url.rstrip("/") + "/"
        parts = urlsplit(self._base)
        self._origin = f"{parts.scheme}://{parts.netloc}"
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth_header = f"Basic {token}"
        self._ssl: bool | None = None if verify_ssl else False

    # -- low level --------------------------------------------------------
    def url_for(self, path: str) -> str:
        return urljoin(self._origin, path)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        headers: dict[str, str] | None = None,
        ok: tuple[int, ...] = (200, 201, 204, 207),
    ) -> tuple[int, dict[str, str], str]:
        hdrs = {"User-Agent": "HomeAssistant-family_tasks", "Authorization": self._auth_header}
        if body is not None and "Content-Type" not in (headers or {}):
            hdrs["Content-Type"] = "application/xml; charset=utf-8"
        hdrs.update(headers or {})
        url = path if path.startswith("http") else self.url_for(path)
        _LOGGER.debug("%s %s", method, urlsplit(url).path)
        try:
            async with self._session.request(
                method,
                url,
                data=body.encode() if body is not None else None,
                headers=hdrs,
                ssl=self._ssl,
                timeout=TIMEOUT,
                allow_redirects=True,
            ) as resp:
                text = await resp.text()
                status = resp.status
                headers_out = {k.lower(): v for k, v in resp.headers.items()}
        except (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError, TimeoutError) as err:
            raise CalDavConnectionError(f"cannot connect: {type(err).__name__}") from None
        except aiohttp.ClientError as err:
            raise CalDavConnectionError(f"request failed: {type(err).__name__}") from None
        if status in (401,):
            raise CalDavAuthError("authentication failed")
        if status == 412:
            raise PreconditionFailed("resource changed on the server")
        if status == 404:
            raise NotFound(f"not found: {unquote(urlsplit(url).path)}")
        if status not in ok:
            if status == 403 and "valid-sync-token" in text:
                raise SyncTokenInvalid("sync token rejected")
            if status in (403, 409) and "sync-token" in text:
                raise SyncTokenInvalid("sync token rejected")
            if status == 403:
                raise CalDavAuthError("access denied")
            raise CalDavError(f"{method} returned HTTP {status}")
        return status, headers_out, text

    async def _propfind(self, path: str, props: str, depth: str) -> ET.Element:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
            'xmlns:cs="http://calendarserver.org/ns/" '
            'xmlns:x1="http://apple.com/ns/ical/">'
            f"<d:prop>{props}</d:prop></d:propfind>"
        )
        _, _, text = await self._request("PROPFIND", path, body=body, headers={"Depth": depth})
        return _parse_xml(text)

    # -- discovery ----------------------------------------------------------
    async def discover(self) -> list[Collection]:
        """Find all calendar collections supporting VTODO."""
        root = await self._propfind(self._base, "<d:current-user-principal/>", "0")
        principal = root.findtext(".//d:current-user-principal/d:href", namespaces=NS)
        if not principal:
            raise CalDavError("server did not return a principal (is this a CalDAV URL?)")
        root = await self._propfind(principal, "<c:calendar-home-set/>", "0")
        home = root.findtext(".//c:calendar-home-set/d:href", namespaces=NS)
        if not home:
            raise CalDavError("no calendar home set")
        root = await self._propfind(
            home,
            "<d:resourcetype/><d:displayname/><c:supported-calendar-component-set/>"
            "<cs:getctag/><d:sync-token/><x1:calendar-color/>",
            "1",
        )
        result: list[Collection] = []
        for response in root.findall("d:response", NS):
            href = response.findtext("d:href", namespaces=NS)
            prop = _ok_prop(response)
            if href is None or prop is None:
                continue
            if prop.find("d:resourcetype/c:calendar", NS) is None:
                continue
            comps = prop.find("c:supported-calendar-component-set", NS)
            names = (
                {c.get("name", "").upper() for c in comps.findall("c:comp", NS)}
                if comps is not None
                else {"VTODO"}  # property absent => all components allowed
            )
            if "VTODO" not in names:
                continue
            result.append(
                Collection(
                    path=href if href.endswith("/") else href + "/",
                    name=prop.findtext("d:displayname", namespaces=NS) or unquote(href.rstrip("/").rsplit("/", 1)[-1]),
                    color=prop.findtext("{http://apple.com/ns/ical/}calendar-color"),
                    ctag=prop.findtext("cs:getctag", namespaces=NS),
                    sync_token=prop.findtext("d:sync-token", namespaces=NS),
                )
            )
        return result

    async def collection_state(self, path: str) -> tuple[str | None, str | None]:
        """Return (ctag, sync-token) for a collection."""
        root = await self._propfind(path, "<cs:getctag/><d:sync-token/>", "0")
        return (
            root.findtext(".//cs:getctag", namespaces=NS),
            root.findtext(".//d:sync-token", namespaces=NS),
        )

    # -- reading -----------------------------------------------------------
    async def fetch_all(self, path: str) -> list[Resource]:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
            '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VTODO"/>'
            "</c:comp-filter></c:filter></c:calendar-query>"
        )
        _, _, text = await self._request("REPORT", path, body=body, headers={"Depth": "1"})
        return _resources(_parse_xml(text))

    async def multiget(self, path: str, hrefs: list[str]) -> list[Resource]:
        if not hrefs:
            return []
        href_xml = "".join(f"<d:href>{_xml_escape(h)}</d:href>" for h in hrefs)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<c:calendar-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f"<d:prop><d:getetag/><c:calendar-data/></d:prop>{href_xml}"
            "</c:calendar-multiget>"
        )
        _, _, text = await self._request("REPORT", path, body=body, headers={"Depth": "1"})
        return _resources(_parse_xml(text))

    async def sync(self, path: str, token: str) -> SyncResult:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:sync-collection xmlns:d="DAV:">'
            f"<d:sync-token>{_xml_escape(token)}</d:sync-token>"
            "<d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop>"
            "</d:sync-collection>"
        )
        _, _, text = await self._request("REPORT", path, body=body)
        root = _parse_xml(text)
        changed: list[str] = []
        deleted: list[str] = []
        for response in root.findall("d:response", NS):
            href = response.findtext("d:href", namespaces=NS)
            if not href or href.rstrip("/") == path.rstrip("/"):
                continue
            status = response.findtext("d:status", namespaces=NS) or ""
            if " 404" in status:
                deleted.append(href)
            else:
                changed.append(href)
        return SyncResult(root.findtext("d:sync-token", namespaces=NS), changed, deleted)

    async def get(self, href: str) -> Resource:
        _, headers, text = await self._request("GET", href, ok=(200,))
        return Resource(href, _strip_etag(headers.get("etag")), text)

    # -- writing -------------------------------------------------------------
    async def put(self, href: str, data: str, *, etag: str | None, create: bool = False) -> str | None:
        """PUT with If-Match (update) or If-None-Match: * (create). Returns new ETag if sent."""
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if create:
            headers["If-None-Match"] = "*"
        elif etag:
            headers["If-Match"] = etag
        _, resp_headers, _ = await self._request("PUT", href, body=data, headers=headers, ok=(200, 201, 204))
        return _strip_etag(resp_headers.get("etag"))

    async def delete(self, href: str, *, etag: str | None) -> None:
        headers = {"If-Match": etag} if etag else {}
        await self._request("DELETE", href, headers=headers, ok=(200, 204))


# ---------------------------------------------------------------------------


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _parse_xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as err:
        raise CalDavError("server returned invalid XML") from err


def _ok_prop(response: ET.Element) -> ET.Element | None:
    for propstat in response.findall("d:propstat", NS):
        status = propstat.findtext("d:status", namespaces=NS) or ""
        if " 200" in status:
            return propstat.find("d:prop", NS)
    return None


def _resources(root: ET.Element) -> list[Resource]:
    out: list[Resource] = []
    for response in root.findall("d:response", NS):
        href = response.findtext("d:href", namespaces=NS)
        prop = _ok_prop(response)
        if href is None or prop is None:
            continue
        data = prop.findtext("c:calendar-data", namespaces=NS)
        out.append(Resource(href, _strip_etag(prop.findtext("d:getetag", namespaces=NS)), data))
    return out
