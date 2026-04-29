"""Web fetch tool — SSRF-guarded URL retrieval via aiohttp."""
import ipaddress
import re
import socket
from urllib.parse import urlparse

import aiohttp

_BLOCKED_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),   # CGNAT / Tailscale
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

MAX_BODY = 256 * 1024   # 256 KB hard cap on response read
DEFAULT_MAX_CHARS = 8000


class SSRFError(Exception):
    pass


def _check_ssrf(host: str) -> None:
    try:
        addr = socket.gethostbyname(host)
    except socket.gaierror as e:
        raise SSRFError(f"DNS resolution failed for {host!r}: {e}")
    ip = ipaddress.ip_address(addr)
    for net in _BLOCKED_NETS:
        if ip in net:
            raise SSRFError(f"Blocked: {addr} is in reserved range {net}")


def _strip_html(html: str) -> str:
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<[^>]+>', ' ', html)
    for ent, char in [('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"')]:
        html = html.replace(ent, char)
    html = re.sub(r'&#\d+;', '', html)
    html = re.sub(r'\s+', ' ', html)
    return html.strip()


async def fetch_url(
    session: aiohttp.ClientSession,
    url: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict:
    """Fetch a URL and return extracted text. Blocks SSRF targets."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"url": url, "error": f"Unsupported scheme: {parsed.scheme!r}"}

    host = parsed.hostname
    if not host:
        return {"url": url, "error": "No hostname in URL"}

    try:
        _check_ssrf(host)
    except SSRFError as e:
        return {"url": url, "error": str(e)}

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {"User-Agent": "agent-001/0.2 (research fetch)"}
        async with session.get(
            url, timeout=timeout, headers=headers, allow_redirects=True, ssl=False
        ) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = await resp.content.read(MAX_BODY)

            if "text/html" in content_type or "text/plain" in content_type:
                text = raw.decode("utf-8", errors="replace")
                if "text/html" in content_type:
                    text = _strip_html(text)
                text = text[:max_chars]
            else:
                text = f"[Non-text content: {content_type}, {len(raw)} bytes]"

            return {
                "url": url,
                "status": resp.status,
                "content_type": content_type,
                "text": text,
            }

    except aiohttp.ClientError as e:
        return {"url": url, "error": f"Request failed: {e}"}
    except Exception as e:
        return {"url": url, "error": f"Unexpected error: {type(e).__name__}: {e}"}
