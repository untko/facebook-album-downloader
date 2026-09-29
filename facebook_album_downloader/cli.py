from __future__ import annotations

import argparse
import asyncio
import sys

from .app import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download Facebook albums and multi-photo posts with Playwright")
    parser.add_argument("source", nargs="?", help="Facebook album/post/photo URL or saved album_urls.json")
    parser.add_argument("-o", "--output", default="downloadedImgs", help="Output root directory")
    parser.add_argument("--login", "--auth", action="store_true", dest="login", help="Open Facebook for interactive login")
    parser.add_argument("--headless", action="store_true", help="Run browser without a visible window")
    parser.add_argument("--state", "--cookies", dest="state_path", default="facebook_cookies.json", help="Playwright storage state file; legacy Selenium cookie arrays are also accepted")
    parser.add_argument("--no-cookies", action="store_true", help="Do not load or save authentication state")
    parser.add_argument("--urls-file", default=None, help="Custom manifest path")
    parser.add_argument("--urls-only", action="store_true", help="Collect URLs without downloading files")
    parser.add_argument("--no-resume", action="store_true", help="Ignore saved manifest/download state")
    parser.add_argument("--max-scrolls", type=int, default=60, help="Maximum source-page scroll attempts")
    parser.add_argument("--scroll-delay", type=float, default=1.0, help="Delay between source-page scrolls")
    parser.add_argument("--max-photos", type=int, default=5000, help="Safety limit for media-viewer traversal")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent image downloads")
    parser.add_argument("--browser", choices=("chromium", "firefox", "webkit"), default="chromium")
    parser.add_argument("--timeout", "--page-timeout", dest="timeout", type=float, default=30.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    source = args.source
    if not source and not args.login:
        source = input("Facebook album/post URL or saved manifest: ").strip()
        if not source:
            raise SystemExit("No source provided")

    try:
        success = asyncio.run(
            run(
                source,
                output=args.output,
                login=args.login,
                headless=args.headless,
                state_path=args.state_path,
                use_state=not args.no_cookies,
                urls_file=args.urls_file,
                resume=not args.no_resume,
                urls_only=args.urls_only,
                max_scrolls=args.max_scrolls,
                scroll_delay=args.scroll_delay,
                max_photos=args.max_photos,
                workers=args.workers,
                browser_name=args.browser,
                timeout_seconds=args.timeout,
            )
        )
    except KeyboardInterrupt:
        print("\nCancelled.")
        success = False
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        success = False
    raise SystemExit(0 if success else 1)
