# Privacy Policy – news-video-uploader

Last updated: October 5, 2026

This privacy policy describes the personal automation tool "news-video-uploader" (source code: https://github.com/meni445/hebrew-news-video), operated by Menachem Swissa.

## What the tool does
The tool builds one daily Hebrew news-summary video and uploads it to the operator's own YouTube channel, "News of the day - חדשות היום" (https://www.youtube.com/@hadashot_hayom). It is used only by the operator. It has no public interface and no other users.

## Use of YouTube API Services
This tool uses YouTube API Services. By using it you agree to the YouTube Terms of Service: https://www.youtube.com/t/terms

Google's Privacy Policy applies to data handled through YouTube API Services: https://policies.google.com/privacy

## Data accessed, used and stored
- The tool authenticates only the operator's own YouTube channel account through OAuth 2.0, using the minimum scope needed to upload videos and set their thumbnails.
- It calls only two API methods: videos.insert (upload the daily video) and thumbnails.set (set its thumbnail).
- It does not read, collect, store or share any data about other YouTube users, their channels, comments or viewing activity.
- The OAuth refresh token for the operator's account is stored only as an encrypted secret in the operator's private GitHub Actions settings and is never published or shared.
- Videos and temporary build files are kept for at most 7 days in the operator's private build storage and then deleted automatically.

## Sharing
No data is sold, shared with or disclosed to third parties.

## Cookies and tracking
The tool has no website and uses no cookies, analytics or tracking.

## Revoking access
The authorized account can revoke the tool's access at any time at https://myaccount.google.com/permissions

## Contact
Questions about this policy: swismeni55@gmail.com
