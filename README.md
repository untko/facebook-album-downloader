# Facebook Album Downloader

A Playwright-based downloader for Facebook albums and multi-photo posts.

This is a clean rewrite built around browser automation rather than Selenium/WebDriver. It is designed for Facebook's current dynamic UI, including posts where only a handful of thumbnails are initially present in the DOM while the post contains many more images.

## What it does

- Downloads albums and multi-photo posts
- Traverses Facebook's media viewer with Playwright instead of assuming the visible thumbnail count is complete
- Persists authenticated browser state for private content your account can access
- Resumes interrupted runs from `album_urls.json`
- Progressively saves discovered media while traversing a post/album
- Reuses existing downloaded files
- Refreshes expired Facebook CDN URLs through the browser
- Downloads multiple images concurrently
- Supports `--urls-only`, headless mode, custom output paths, and saved manifests
- Reads the legacy manifest formats used by the original project
- Reads legacy Selenium cookie arrays and migrates them to Playwright storage state on the next save

## Install

Python 3.10+ is recommended.

```bash
git clone https://github.com/untko/facebook-album-downloader.git
cd facebook-album-downloader
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .
playwright install chromium
```

## Usage

Download an album or multi-photo post:

```bash
facebook-album-downloader "https://www.facebook.com/..."
```

The old entry point is also kept:

```bash
python albumDownloader.py "https://www.facebook.com/..."
```

Authenticate interactively and save the browser session:

```bash
facebook-album-downloader --login
```

Then download content visible to that account:

```bash
facebook-album-downloader "https://www.facebook.com/..." --headless
```

Extract URLs without downloading:

```bash
facebook-album-downloader "https://www.facebook.com/..." --urls-only
```

Resume directly from a manifest:

```bash
facebook-album-downloader downloadedImgs/My_Album/album_urls.json
```

Custom output folder:

```bash
facebook-album-downloader "https://www.facebook.com/..." --output my_downloads
```

## Why the collector does not trust the thumbnail count

Facebook often renders only the first few photos of a large post. A post containing 70+ images can expose only about five photo links in the initial DOM. Counting those links therefore underestimates the post.

The Playwright collector uses several stages:

1. Collect visible photo links while the source page is loaded.
2. Open the first Facebook photo URL.
3. Read the full-resolution media from the viewer.
4. Advance the viewer with keyboard/navigation controls.
5. Continue until a media ID repeats, the Facebook `set` changes, or the viewer can no longer advance.
6. Fall back to individually visiting any discovered links that were not resolved by the viewer.

For posts, the `set=pcb...` identifier helps keep traversal inside the same post. For albums, the `set=a...` identifier serves the same role.

## Main options

```text
-o, --output PATH          Output root (default: downloadedImgs)
--login, --auth            Interactive Facebook login
--headless                 Run browser without a visible window
--state PATH               Playwright storage-state file
--cookies PATH             Alias kept for compatibility
--no-cookies               Do not load/save authentication state
--urls-file PATH           Custom manifest location
--urls-only                Extract media URLs without downloading files
--no-resume                Ignore previously downloaded files/manifest state
--max-scrolls N            Maximum source-page scroll attempts
--max-photos N             Safety limit for viewer traversal
--workers N                Concurrent image downloads
--browser NAME             chromium, firefox, or webkit
--timeout SECONDS          Navigation/request timeout
```

## Notes

- Facebook changes its frontend frequently. The collector intentionally relies on media IDs, viewer state, image dimensions, and structural navigation rather than generated CSS class names.
- Private content can only be downloaded when the logged-in Facebook account already has permission to view it.
- Use this only for content you are authorized to access and download.
