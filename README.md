# Facebook Album Downloader

A Playwright-based downloader for Facebook albums and multi-photo posts.

This project is a clean rewrite of the original Selenium/WebDriver implementation. It is designed for Facebook's dynamic UI, including posts where only a few thumbnails are initially visible while many more images are hidden behind a `+N` tile.

## Features

- Downloads Facebook albums and multi-photo posts
- Traverses the Facebook media viewer with Playwright
- Scopes `+N` discovery to the post collage that owns the overflow tile
- Locks multi-photo post traversal to its `set=pcb...` media set
- Uses rendered media identity to avoid duplicate downloads caused by stale viewer URLs
- Captures GraphQL photo records as an additional discovery signal
- Refuses to download an incomplete or unrelated media set when an expected count is known
- Persists authenticated browser state
- Resumes interrupted runs from `album_urls.json`
- Progressively saves manifests while traversing media
- Refreshes expired Facebook CDN URLs
- Downloads images concurrently
- Supports URL-only extraction, headless mode, custom output paths, and saved manifests
- Reads legacy manifest formats and legacy Selenium cookie arrays

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

The legacy entry point is also available:

```bash
python albumDownloader.py "https://www.facebook.com/..."
```

Authenticate interactively once and save the Playwright session:

```bash
facebook-album-downloader --login
```

Then reuse that session in later runs:

```bash
facebook-album-downloader "https://www.facebook.com/..." --headless
```

Extract URLs without downloading:

```bash
facebook-album-downloader "https://www.facebook.com/..." --urls-only
```

Resume from a saved manifest:

```bash
facebook-album-downloader downloadedImgs/My_Album/album_urls.json
```

Choose a custom output folder:

```bash
facebook-album-downloader "https://www.facebook.com/..." --output my_downloads
```

## How multi-photo posts are handled

Facebook may expose only a handful of photo anchors in the initial DOM. Large posts can place the remaining media behind a `+N` overlay, so the collector does not treat the visible anchor count as the total.

For posts with overflow media, the collector:

1. Locates the visible `+N` tile.
2. Finds the photo collage that owns that tile.
3. Derives and locks the post's `set=pcb...` media-set identifier.
4. Computes the expected media count using the collage and Facebook's `+N` semantics.
5. Opens the media viewer from that scoped collage.
6. Advances through the viewer while tracking the rendered image itself rather than relying only on the browser URL.
7. Deduplicates by media identity and filters out records from unrelated sets.
8. Uses GraphQL photo records only as a supplemental discovery source.
9. Refuses to download when traversal is incomplete and an expected total is known.

Albums use the same viewer-based extraction approach, with their own Facebook set identifiers when available.

## Main options

```text
-o, --output PATH          Output root (default: downloadedImgs)
--login, --auth            Interactive Facebook login
--headless                 Run browser without a visible window
--state PATH               Playwright storage-state file
--cookies PATH             Compatibility alias for --state
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

- Facebook changes its frontend frequently. The collector relies on media identity, viewer state, structural navigation, and Facebook media-set boundaries rather than generated CSS class names.
- Private content can only be downloaded when the logged-in account already has permission to view it.
- Authentication state files and downloaded media are ignored by Git.
- Use the tool only for content you are authorized to access and download.
