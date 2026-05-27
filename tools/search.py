"""SearXNG search tool — proxies queries to the self-hosted instance."""
import logging

import aiohttp

log = logging.getLogger("agent-001.tools.search")


async def search(
    session: aiohttp.ClientSession,
    query: str,
    searxng_url: str,
    limit: int = 5,
) -> dict:
    """
    Search via SearXNG JSON API.
    Returns: {"query": str, "results": [{title, url, snippet}]} or {"error": str}
    """
    endpoint = searxng_url.rstrip("/") + "/search"
    params = {
        "q": query,
        "format": "json",
        "categories": "general",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(endpoint, params=params, timeout=timeout) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning("SearXNG returned %s: %s", resp.status, body[:200])
                return {"query": query, "error": f"SearXNG returned {resp.status}"}

            data = await resp.json(content_type=None)
            results = data.get("results", [])[:limit]
            return {
                "query": query,
                "results": [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": r.get("content", ""),
                    }
                    for r in results
                ],
            }

    except aiohttp.ClientError as e:
        log.warning("SearXNG request failed: %s", e)
        return {"query": query, "error": f"SearXNG unreachable: {e}"}
    except Exception as e:
        log.warning("SearXNG unexpected error: %s", e)
        return {"query": query, "error": f"Unexpected error: {type(e).__name__}: {e}"}
