# Hebrew Daily News Video (news-video-uploader)

A personal automation tool that produces one short daily Hebrew news-summary video for the YouTube channel **News of the day - חדשות היום**: https://www.youtube.com/@hadashot_hayom

Once a day a scheduled GitHub Actions job:
1. Collects the last 24 hours of posts from public Hebrew news channels on Telegram.
2. Writes a summary script with AI (reports are attributed to their sources; unconfirmed reports are labeled as such).
3. Narrates it with an AI voice and renders a video with an AI-generated presenter (disclosed as AI-generated).
4. Uploads the video to the operator's own YouTube channel using YouTube API Services (videos.insert, thumbnails.set).

The tool is used only by its operator and does not access any other YouTube user's data.

- Privacy Policy: [PRIVACY.md](PRIVACY.md)
- Terms of Service: [TERMS.md](TERMS.md)
- This tool uses YouTube API Services. YouTube Terms of Service: https://www.youtube.com/t/terms
- Google Privacy Policy: https://policies.google.com/privacy
- Contact: swismeni55@gmail.com
