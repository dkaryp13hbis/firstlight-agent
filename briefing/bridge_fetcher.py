"""
Fetches briefing data from a hotel's HTTP bridge (via Cloudflare Tunnel)
instead of connecting directly to SQL Server.
Used by Railway cloud processor.
"""
import requests


def fetch_from_bridge(bridge_url: str, bridge_secret: str, timeout: int = 30) -> dict:
    url = bridge_url.rstrip("/") + "/fetch"
    r = requests.get(
        url,
        headers={"x-bridge-secret": bridge_secret},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Bridge error: {data['error']}")
    return data
