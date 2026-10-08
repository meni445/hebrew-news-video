"""Skip the run if the channel already published a video recently.

The workflow has several backup schedule times because GitHub often starts scheduled runs
hours late. The first one that runs makes the video; the others see it and stop here.
Writes skip=true/false to $GITHUB_OUTPUT. On any error it does NOT skip.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

HOURS = float(os.getenv("GUARD_HOURS", "14"))
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"


def output(skip, reason):
    print(f"{'SKIP' if skip else 'RUN'}: {reason}")
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"skip={'true' if skip else 'false'}\n")
    sys.exit(0)


def main():
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        output(False, "started by hand - always run")
    try:
        r = requests.post(TOKEN_URL, data={
            "client_id": os.environ["YT_CLIENT_ID"],
            "client_secret": os.environ["YT_CLIENT_SECRET"],
            "refresh_token": os.environ["YT_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        }, timeout=60)
        r.raise_for_status()
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

        r = requests.get(f"{API}/channels", params={"part": "contentDetails", "mine": "true"},
                         headers=headers, timeout=60)
        r.raise_for_status()
        uploads = r.json()["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

        r = requests.get(f"{API}/playlistItems",
                         params={"part": "contentDetails", "playlistId": uploads, "maxResults": 5},
                         headers=headers, timeout=60)
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception as e:
        output(False, f"could not check the channel ({e})")

    times = [datetime.fromisoformat(i["contentDetails"]["videoPublishedAt"].replace("Z", "+00:00"))
             for i in items if i.get("contentDetails", {}).get("videoPublishedAt")]
    if not times:
        output(False, "no videos on the channel yet")
    last = max(times)
    age = datetime.now(timezone.utc) - last
    hours = age.total_seconds() / 3600
    if age < timedelta(hours=HOURS):
        output(True, f"last video published {hours:.1f} h ago (< {HOURS:g} h)")
    output(False, f"last video published {hours:.1f} h ago")


if __name__ == "__main__":
    main()
