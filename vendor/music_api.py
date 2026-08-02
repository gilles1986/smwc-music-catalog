"""SMWC Music API client for SMWCentral.net.

Provides a thread-safe API client for browsing, searching, and retrieving
music entries from the SMWCentral.net music section.
"""

import html
import re
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = "https://www.smwcentral.net/ajax.php"
_DEFAULT_SECTION = "smwmusic"
_DEFAULT_TIMEOUT = 30
_DEFAULT_DELAY = 0.8
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_PER_PAGE = 50  # Server-fixed page size


class SortField(Enum):
    """Supported sort fields for the SMWC API."""
    DATE = "date"
    DOWNLOADS = "downloads"
    NAME = "name"
    SIZE = "size"


class SortDirection(Enum):
    """Sort direction."""
    ASC = "asc"
    DESC = "desc"


class MusicType(Enum):
    """Music entry type filter values."""
    SONG = "song"
    SOUNDTRACK = "soundtrack"
    SOUND_EFFECT = "sound_effect"
    MISC = "misc"


class SampleUsage(Enum):
    """Sample usage filter values."""
    NONE = "none"
    LIGHT = "light"
    MANY = "many"


class SourceType(Enum):
    """Source type filter values."""
    PORT = "port"
    ORIGINAL = "original"
    REMIX = "remix"


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class Author:
    """Represents a music entry author."""
    id: Optional[int]
    name: str


@dataclass
class MusicEntry:
    """Represents a single music entry from the SMWC API."""
    id: int
    name: str
    section: str
    time: int
    moderated: bool
    authors: List[Author]
    tags: List[str]
    rating: float
    size: int
    downloads: int
    download_url: str
    obsoleted_by: Optional[int]

    # Parsed fields
    type: str = ""
    samples: str = ""
    source: str = ""
    duration: str = ""
    description: str = ""
    raw_description: str = ""
    aram_size: str = ""

    @classmethod
    def from_api_data(cls, data: Dict[str, Any]) -> "MusicEntry":
        """Create a MusicEntry from raw API response data.

        Args:
            data: Dictionary from the API ``data`` array.

        Returns:
            Populated ``MusicEntry`` instance.
        """
        authors = [
            Author(id=a.get("id"), name=a.get("name", ""))
            for a in data.get("authors", [])
        ]

        raw_fields = data.get("raw_fields") or {}
        fields = data.get("fields") or {}

        description_html = fields.get("description", "")
        raw_description = raw_fields.get("description", "")

        return cls(
            id=data.get("id", 0),
            name=data.get("name", "").strip(),
            section=data.get("section", ""),
            time=data.get("time", 0),
            moderated=data.get("moderated", False),
            authors=authors,
            tags=data.get("tags") or [],
            rating=float(data.get("rating", 0) or 0),
            size=data.get("size", 0),
            downloads=data.get("downloads", 0),
            download_url=data.get("download_url", ""),
            obsoleted_by=data.get("obsoleted_by"),
            type=raw_fields.get("type", ""),
            samples=raw_fields.get("samples", ""),
            source=raw_fields.get("source", ""),
            duration=fields.get("duration", "").strip(),
            description=_strip_bbcode_and_html(description_html),
            raw_description=raw_description,
            aram_size=fields.get("size", ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dictionary suitable for JSON."""
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MusicEntry":
        """Deserialise from a dictionary (e.g. loaded from JSON)."""
        authors = [
            Author(id=a.get("id"), name=a.get("name", ""))
            for a in data.get("authors", [])
        ]
        return cls(
            id=data.get("id", 0),
            name=data.get("name", ""),
            section=data.get("section", ""),
            time=data.get("time", 0),
            moderated=data.get("moderated", False),
            authors=authors,
            tags=data.get("tags") or [],
            rating=float(data.get("rating", 0) or 0),
            size=data.get("size", 0),
            downloads=data.get("downloads", 0),
            download_url=data.get("download_url", ""),
            obsoleted_by=data.get("obsoleted_by"),
            type=data.get("type", ""),
            samples=data.get("samples", ""),
            source=data.get("source", ""),
            duration=data.get("duration", ""),
            description=data.get("description", ""),
            raw_description=data.get("raw_description", ""),
            aram_size=data.get("aram_size", ""),
        )

    @property
    def author_names(self) -> str:
        """Return comma-separated author names."""
        return ", ".join(a.name for a in self.authors if a.name)

    @property
    def duration_seconds(self) -> int:
        """Parse duration string (e.g. '2:08') into total seconds.

        Returns:
            Duration in seconds, or 0 if parsing fails.
        """
        try:
            parts = self.duration.strip().split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            pass
        return 0


@dataclass
class SearchResult:
    """Container for a paginated search result."""
    total: int
    per_page: int
    current_page: int
    last_page: int
    entries: List[MusicEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class APIError(Exception):
    """Base exception for API errors."""


class APIConnectionError(APIError):
    """Raised when a network connection cannot be established."""


class APITimeoutError(APIError):
    """Raised when a request times out."""


class APIResponseError(APIError):
    """Raised when the API returns an unexpected response."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BBCODE_RE = re.compile(
    r"\[/?(?:b|i|u|s|url|img|color|size|quote|spoiler|user|code)"
    r"(?:=[^\]]*)?]",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def _strip_bbcode_and_html(text: str) -> str:
    """Remove BBCode tags and HTML from a description string.

    Args:
        text: Raw description text potentially containing BBCode / HTML.

    Returns:
        Plain text string.
    """
    if not text:
        return ""

    # Replace <br> variants with newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)

    # Strip remaining HTML tags
    text = _HTML_TAG_RE.sub("", text)

    # Strip BBCode
    text = _BBCODE_RE.sub("", text)

    # Decode HTML entities
    text = html.unescape(text)

    # Normalise whitespace
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    text = _MULTI_SPACE_RE.sub(" ", text)

    return text.strip()


# ---------------------------------------------------------------------------
# Main API Client
# ---------------------------------------------------------------------------

class MusicAPI:
    """Thread-safe client for the SMWCentral.net music API.

    Handles rate limiting, retries with exponential backoff,
    pagination, and client-side filtering.

    Args:
        base_url: API base URL (default: ``https://www.smwcentral.net/ajax.php``).
        delay: Minimum seconds between requests for rate limiting.
        timeout: HTTP request timeout in seconds.
        max_retries: Maximum number of retry attempts on transient errors.
        session: Optional ``requests.Session`` for dependency injection / testing.
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        delay: float = _DEFAULT_DELAY,
        timeout: int = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        session: Optional[requests.Session] = None,
    ):
        self._base_url = base_url
        self._delay = delay
        self._timeout = timeout
        self._max_retries = max_retries
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._last_request_time: float = 0.0
        self.log_callback: Optional[Callable[[str], None]] = None

    def _log(self, message: str) -> None:
        """Invoke the log callback if registered."""
        if self.log_callback:
            try:
                self.log_callback(message)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(
        self,
        page: int = 1,
        name: Optional[str] = None,
        sort_by: SortField = SortField.DATE,
        sort_dir: SortDirection = SortDirection.DESC,
        type_filter: Optional[MusicType] = None,
        samples_filter: Optional[SampleUsage] = None,
        source_filter: Optional[SourceType] = None,
        tags_filter: Optional[List[str]] = None,
        author: Optional[str] = None,
        tags: Optional[str] = None,
        description: Optional[str] = None,
    ) -> SearchResult:
        """Search the SMWC music section with optional filters.

        Server-side filtering is used for ``name``, ``author``, ``tags``,
        and ``description``.  The remaining filters (type, samples,
        source, tags_filter) are applied client-side after fetching.

        Args:
            page: 1-based page number.
            name: Search by music name (server-side).
            sort_by: Field to sort by.
            sort_dir: Sort direction (asc / desc).
            type_filter: Filter by music type (client-side).
            samples_filter: Filter by sample usage (client-side).
            source_filter: Filter by source type (client-side).
            tags_filter: Filter by tags – entries must have *all* listed tags (client-side).
            author: Search by author name (server-side).
            tags: Search by tags string (server-side, comma-separated).
            description: Search by description text (server-side).

        Returns:
            ``SearchResult`` with matching entries and pagination info.

        Raises:
            APIConnectionError: On network failures after retries.
            APITimeoutError: On request timeout after retries.
            APIResponseError: On unexpected API responses.
        """
        params = self._build_params(
            page, name, sort_by, sort_dir,
            author=author, tags=tags, description=description,
        )
        data = self._request(params)
        result = self._parse_response(data)

        # Apply client-side filters
        has_client_filter = any([
            type_filter, samples_filter, source_filter, tags_filter
        ])
        if has_client_filter:
            result.entries = self._apply_filters(
                result.entries,
                type_filter=type_filter,
                samples_filter=samples_filter,
                source_filter=source_filter,
                tags_filter=tags_filter,
            )

        return result

    def get_entry(self, entry_id: int) -> Optional[MusicEntry]:
        """Fetch a single music entry by its ID.

        This performs a search across all pages is **not** efficient.
        Use sparingly – for bulk lookups, prefer ``search()``.

        Note: The SMWC API does not provide a single-entry endpoint, so
        we must search and filter client-side.

        Args:
            entry_id: The numeric ID of the entry.

        Returns:
            ``MusicEntry`` if found, ``None`` otherwise.

        Raises:
            APIConnectionError: On network failures after retries.
            APITimeoutError: On request timeout after retries.
            APIResponseError: On unexpected API responses.
        """
        params = self._build_params(page=1)
        # Fetch first page to check
        data = self._request(params)

        all_data = data.get("data", [])
        for item in all_data:
            if item.get("id") == entry_id:
                return MusicEntry.from_api_data(item)

        return None

    def fetch_all_pages(
        self,
        name: Optional[str] = None,
        sort_by: SortField = SortField.DATE,
        sort_dir: SortDirection = SortDirection.DESC,
        max_pages: int = 0,
        on_progress: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        existing_ids: Optional[set[int]] = None,
    ) -> List[MusicEntry]:
        """Fetch multiple pages of results.

        Args:
            name: Optional name search filter.
            sort_by: Field to sort by.
            sort_dir: Sort direction.
            max_pages: Maximum pages to fetch (0 = all pages).
            on_progress: Optional callback ``(current_page, total_pages)``.
            cancel_event: Optional threading.Event to abort execution.
            existing_ids: Optional set of database entry IDs to skip further sync pages.

        Returns:
            List of all ``MusicEntry`` objects fetched.

        Raises:
            APIConnectionError: On network failures after retries.
            APITimeoutError: On request timeout after retries.
            APIResponseError: On unexpected API responses.
        """
        first_result = self.search(
            page=1, name=name, sort_by=sort_by, sort_dir=sort_dir,
        )
        total_pages = first_result.last_page

        if existing_ids and first_result.entries:
            page_1_ids = {e.id for e in first_result.entries}
            if page_1_ids.issubset(existing_ids):
                self._log("All entries on page 1 are already in the local database.")
                if on_progress:
                    try:
                        on_progress(1, total_pages)
                    except Exception:
                        pass
                return []

        entries = list(first_result.entries)
        total_pages = first_result.last_page

        if on_progress:
            try:
                on_progress(1, total_pages)
            except Exception:
                pass

        pages_to_fetch = total_pages if max_pages <= 0 else min(max_pages, total_pages)

        for page in range(2, pages_to_fetch + 1):
            if cancel_event and cancel_event.is_set():
                break
            result = self.search(
                page=page, name=name, sort_by=sort_by, sort_dir=sort_dir,
            )

            if existing_ids and result.entries:
                page_ids = {e.id for e in result.entries}
                if page_ids.issubset(existing_ids):
                    self._log(f"Page {page} contains only already-known entries. Stopping sync.")
                    break

            entries.extend(result.entries)

            if on_progress:
                try:
                    on_progress(page, total_pages)
                except Exception:
                    pass

        return entries

    @property
    def delay(self) -> float:
        """Current rate-limit delay in seconds."""
        return self._delay

    @delay.setter
    def delay(self, value: float) -> None:
        """Set rate-limit delay. Must be >= 0."""
        self._delay = max(0.0, float(value))

    @property
    def timeout(self) -> int:
        """Current request timeout in seconds."""
        return self._timeout

    @timeout.setter
    def timeout(self, value: int) -> None:
        """Set request timeout. Must be > 0."""
        self._timeout = max(1, int(value))

    # ------------------------------------------------------------------
    # Internal Methods
    # ------------------------------------------------------------------

    def _build_params(
        self,
        page: int = 1,
        name: Optional[str] = None,
        sort_by: SortField = SortField.DATE,
        sort_dir: SortDirection = SortDirection.DESC,
        author: Optional[str] = None,
        tags: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build query parameters for the API request.

        Args:
            page: Page number (1-based).
            name: Optional name filter (server-side).
            sort_by: Sort field.
            sort_dir: Sort direction.
            author: Optional author filter (server-side).
            tags: Optional tags filter (server-side, comma-separated).
            description: Optional description filter (server-side).

        Returns:
            Dictionary of query parameters.
        """
        params: Dict[str, Any] = {
            "a": "getsectionlist",
            "s": _DEFAULT_SECTION,
            "u": 0,
            "n": max(1, page),
            "o": sort_by.value,
            "d": sort_dir.value,
        }
        if name:
            params["f[name]"] = name
        if author:
            params["f[author]"] = author
        if tags:
            params["f[tags]"] = tags
        if description:
            params["f[description]"] = description
        return params

    def _rate_limit(self) -> None:
        """Enforce rate limiting between requests.

        Thread-safe: uses a lock to ensure consistent timing across threads.
        """
        with self._lock:
            if self._delay <= 0:
                return
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self._delay:
                wait_time = self._delay - elapsed
                self._log(f"Waiting {wait_time:.1f}s (rate limiting)...")
                time.sleep(wait_time)
            self._last_request_time = time.monotonic()

    def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an HTTP GET request with retries and rate limiting.

        Args:
            params: Query parameters for the request.

        Returns:
            Parsed JSON response as a dictionary.

        Raises:
            APIConnectionError: After all retries fail due to connection issues.
            APITimeoutError: After all retries fail due to timeout.
            APIResponseError: On HTTP errors or invalid JSON.
        """
        last_error: Optional[Exception] = None

        for attempt in range(self._max_retries):
            self._rate_limit()

            page_num = params.get("n", 1) if params else 1
            # Build representative url for printing
            req = requests.Request("GET", self._base_url, params=params)
            prep_url = self._session.prepare_request(req).url or self._base_url
            self._log(f"Requesting metadata page {page_num}: {prep_url}")

            try:
                response = self._session.get(
                    self._base_url,
                    params=params,
                    timeout=self._timeout,
                )
                
                # Dynamic rate limit check
                rate_remaining = response.headers.get("x-ratelimit-remaining")
                rate_limit = response.headers.get("x-ratelimit-limit")
                if rate_remaining and isinstance(rate_remaining, (str, int)):
                    try:
                        rem = int(rate_remaining)
                        limit = int(rate_limit) if rate_limit else 60
                        if limit >= 5 and rem < 5:
                            self._log(f"Rate limit remaining: {rem} of {limit}. Starting 5s cooldown pause...")
                            time.sleep(5.0)
                    except ValueError:
                        pass

                response.raise_for_status()

                data = response.json()
                if not isinstance(data, dict):
                    raise APIResponseError(
                        f"Unexpected response type: {type(data).__name__}"
                    )
                total_entries = len(data.get("data", []))
                self._log(f"Result page {page_num}: success ({total_entries} entries loaded)")
                return data

            except requests.exceptions.ConnectionError as e:
                self._log(f"Connection error on page {page_num} (attempt {attempt + 1}/{self._max_retries}): {e}")
                last_error = APIConnectionError(
                    f"Connection failed (attempt {attempt + 1}/{self._max_retries}): {e}"
                )
            except requests.exceptions.Timeout as e:
                self._log(f"Timeout on page {page_num} (attempt {attempt + 1}/{self._max_retries}): {e}")
                last_error = APITimeoutError(
                    f"Request timed out (attempt {attempt + 1}/{self._max_retries}): {e}"
                )
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else None
                self._log(f"HTTP error {status_code} on page {page_num} (attempt {attempt + 1}/{self._max_retries}): {e}")
                # Don't retry on client errors (4xx) except 429 (rate limit)
                if status_code is not None and 400 <= status_code < 500 and status_code != 429:
                    raise APIResponseError(
                        f"HTTP {status_code}: {e}", status_code=status_code
                    ) from e
                last_error = APIResponseError(
                    f"HTTP error (attempt {attempt + 1}/{self._max_retries}): {e}",
                    status_code=status_code,
                )
            except ValueError as e:
                self._log(f"Invalid JSON response on page {page_num}: {e}")
                raise APIResponseError(f"Invalid JSON response: {e}") from e

            # Exponential backoff: 1s, 2s, 4s, ...
            if attempt < self._max_retries - 1:
                backoff = 2 ** attempt
                self._log(f"Waiting {backoff}s backoff before next attempt...")
                time.sleep(backoff)

        # All retries exhausted
        raise last_error  # type: ignore[misc]

    def _parse_response(self, data: Dict[str, Any]) -> SearchResult:
        """Parse raw API response into a SearchResult.

        Args:
            data: Raw JSON response dictionary.

        Returns:
            Populated ``SearchResult`` instance.
        """
        entries = [
            MusicEntry.from_api_data(item)
            for item in data.get("data", [])
        ]

        return SearchResult(
            total=data.get("total", 0),
            per_page=data.get("per_page", _DEFAULT_PER_PAGE),
            current_page=data.get("current_page", 1),
            last_page=data.get("last_page", 1),
            entries=entries,
        )

    @staticmethod
    def _apply_filters(
        entries: List[MusicEntry],
        type_filter: Optional[MusicType] = None,
        samples_filter: Optional[SampleUsage] = None,
        source_filter: Optional[SourceType] = None,
        tags_filter: Optional[List[str]] = None,
    ) -> List[MusicEntry]:
        """Apply client-side filters to a list of entries.

        Args:
            entries: List of entries to filter.
            type_filter: Filter by music type.
            samples_filter: Filter by sample usage.
            source_filter: Filter by source type.
            tags_filter: Filter by tags (all must match).

        Returns:
            Filtered list of entries.
        """
        result = entries

        if type_filter is not None:
            result = [e for e in result if e.type == type_filter.value]

        if samples_filter is not None:
            result = [e for e in result if e.samples == samples_filter.value]

        if source_filter is not None:
            result = [e for e in result if e.source == source_filter.value]

        if tags_filter:
            lower_tags = [t.lower() for t in tags_filter]
            result = [
                e for e in result
                if all(tag in [t.lower() for t in e.tags] for tag in lower_tags)
            ]

        return result
