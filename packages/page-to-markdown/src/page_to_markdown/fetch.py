"""Input acquisition for page-to-markdown: HTTP fetch and local file reading."""

import socket
import urllib.error
import urllib.request

# Hard timeout for the entire fetch, in seconds.
FETCH_TIMEOUT = 30

# Streaming byte cap to prevent unbounded downloads.
FETCH_BYTE_CAP = 10 * 1024 * 1024  # 10 MB

# Browser-like headers so servers return real HTML, not bot-block pages.
BROWSER_HEADERS = {
	"User-Agent": (
		"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
		"AppleWebKit/537.36 (KHTML, like Gecko) "
		"Chrome/120.0.0.0 Safari/537.36"
	),
	"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
	"Accept-Language": "en-GB,en;q=0.9",
}


class FetchError(Exception):
	"""Raised when a fetch or file read fails."""


def fetch_url(url):
	"""Fetch a URL with browser-like headers, timeout, and byte cap.

	Returns the response body as a string. Raises FetchError on timeout,
	HTTP error, or byte-cap exceedance.
	"""
	req = urllib.request.Request(url, headers=BROWSER_HEADERS)
	try:
		with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
			chunks = []
			total = 0
			while True:
				chunk = resp.read(8192)
				if not chunk:
					break
				total += len(chunk)
				if total > FETCH_BYTE_CAP:
					raise FetchError(
						f"fetch exceeded {FETCH_BYTE_CAP} byte cap from {url}"
					)
				chunks.append(chunk)
			return b"".join(chunks).decode("utf-8", errors="replace")
	except socket.timeout:
		raise FetchError(f"fetch timed out after {FETCH_TIMEOUT}s for {url}")
	except urllib.error.HTTPError as e:
		raise FetchError(f"HTTP {e.code} {e.reason} for {url}")
	except urllib.error.URLError as e:
		raise FetchError(f"could not fetch {url}: {e.reason}")


def read_file(path):
	"""Read a local file and return its content as a string."""
	try:
		with open(path, "r", encoding="utf-8", errors="replace") as f:
			return f.read()
	except OSError as e:
		raise FetchError(str(e))
