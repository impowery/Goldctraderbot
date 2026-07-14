#!/usr/bin/env python3
"""Free news filter — fetches Forex Factory economic calendar JSON.
No API key needed. Checks if any USD high-impact event is within 15 min window.
URL: https://nfs.faireconomy.media/ff_calendar_thisweek.json
"""
import json
import urllib.request
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))

# Cache events for 1 hour to avoid repeated requests
_events_cache = None
_cache_time = 0
_CACHE_TTL = 3600  # 1 hour

def get_high_impact_events():
    """Get this week's high-impact USD events from Forex Factory (free JSON)."""
    global _events_cache, _cache_time
    
    now = int(datetime.now(timezone.utc).timestamp())
    if _events_cache is not None and (now - _cache_time) < _CACHE_TTL:
        return _events_cache
    
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        
        high_impact = []
        for event in data:
            impact = event.get("impact", "").lower()
            country = event.get("country", "").upper()
            if impact == "high" and country == "USD":
                date_str = event.get("date", "")
                if date_str:
                    try:
                        # Forex Factory dates are in ET (America/New_York)
                        # Format: 2026-07-14T08:30:00-04:00
                        event_time = datetime.fromisoformat(date_str)
                        # Convert to UTC
                        event_time_utc = event_time.astimezone(timezone.utc)
                        high_impact.append({
                            "time": event_time_utc,
                            "name": event.get("title", "Unknown"),
                            "country": country,
                            "impact": impact,
                        })
                    except Exception:
                        continue
        
        _events_cache = high_impact
        _cache_time = now
        return high_impact
    except Exception as e:
        print(f"[News] Error fetching events: {e}")
        return _events_cache if _events_cache else []

def is_news_blackout():
    """Check if current time is within 15 min of a high-impact USD news event.
    Returns (bool: blocked, str: reason)."""
    events = get_high_impact_events()
    if not events:
        return False, "No high-impact USD events"
    
    now = datetime.now(timezone.utc)
    for event in events:
        event_time = event["time"]
        diff = abs((now - event_time).total_seconds())
        if diff < 15 * 60:  # 15 minutes before or after
            mins = int(diff / 60)
            before_after = "before" if now < event_time else "after"
            return True, f"News blackout: {event['name']} ({mins} min {before_after})"
    
    return False, "Clear of news"

if __name__ == "__main__":
    blocked, msg = is_news_blackout()
    print(f"Blocked: {blocked}")
    print(f"Message: {msg}")
    events = get_high_impact_events()
    print(f"\nThis week's high-impact USD events ({len(events)}):")
    for e in events:
        msk_time = e["time"].astimezone(MSK)
        print(f"  {msk_time.strftime('%a %H:%M')} MSK — {e['name']}")
