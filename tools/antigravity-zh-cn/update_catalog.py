"""Read the official Antigravity App macOS update channel, without changing files.

The feed URL and architecture-specific channel names match the official desktop
app's app-update.yml and updater.js. A rollout channel can lag the public download
page: get_latest() reports the version offered by this channel, not a scraped
website version. It never queries GitHub or treats a release tag as compatibility
proof. Localization compatibility must be checked against the downloaded app.

Public API:
    get_latest(architecture=None, *, timeout=10.0, installed_version=None)
        -> {version, url, sha512, size, architecture, feed_url}
    version_tuple("2.29.0") -> (2, 29, 0)
    compare_versions(installed_version_or_None, available_version)
        -> "not_installed" | "update_available" | "up_to_date" | "ahead"

Failures raise CatalogError with a machine-readable ``kind``. No failure is
reported as "up_to_date" and no cached version is silently substituted.
"""

import base64
import binascii
import ctypes
import errno
import http.client
import math
from pathlib import Path
import platform
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


FEED_BASE = "https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/manifest/"
MAX_FEED_BYTES = 64 * 1024
USER_AGENT = "antigravity-local-zh-cn-update-check/1"
_VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_ARCHITECTURES = {
    "arm64": "arm64", "aarch64": "arm64",
    "x64": "x64", "x86_64": "x64", "amd64": "x64",
}
_TOP_KEYS = {"version", "files", "path", "sha512", "releaseDate", "stagingPercentage"}
_FILE_KEYS = {"url", "sha512", "size", "blockMapSize"}


class CatalogError(ValueError):
    """A failed catalog lookup; kind is suitable for a user-facing status map."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def version_tuple(version):
    """Parse a stable x.y.z version strictly; reject prefixes and prereleases."""
    if not isinstance(version, str) or len(version) > 80:
        raise CatalogError("invalid_version", "Version must be a stable x.y.z string")
    match = _VERSION_RE.fullmatch(version)
    if not match:
        raise CatalogError("invalid_version", "Version must be a stable x.y.z string")
    return tuple(int(part) for part in match.groups())


def compare_versions(installed_version, available_version):
    """Compare installed and channel versions without implying patch compatibility."""
    available = version_tuple(available_version)
    if installed_version is None:
        return "not_installed"
    installed = version_tuple(installed_version)
    if installed < available:
        return "update_available"
    if installed > available:
        return "ahead"
    return "up_to_date"


def _is_rosetta():
    # Query this Python process rather than a spawned universal command, which
    # might run natively. Apple documents sysctl.proc_translated as 1 for Rosetta
    # and ENOENT on systems without that flag.
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.sysctlbyname
        function.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        function.restype = ctypes.c_int
        translated = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(translated))
        result = function(b"sysctl.proc_translated", ctypes.byref(translated), ctypes.byref(size), None, 0)
        if result == 0 and size.value == ctypes.sizeof(translated) and translated.value in (0, 1):
            return translated.value == 1
        if result != 0 and ctypes.get_errno() == errno.ENOENT:
            return False
    except (OSError, AttributeError):
        pass
    raise CatalogError("unsupported_architecture", "Could not determine the native macOS architecture; specify arm64 or x64")


def normalize_architecture(architecture=None):
    """Return arm64/x64; automatic detection prefers native ARM under Rosetta.

    An explicit x64 argument is honored, including on an Apple Silicon host.
    """
    value = platform.machine() if architecture is None else architecture
    if not isinstance(value, str) or value.lower() not in _ARCHITECTURES:
        raise CatalogError("unsupported_architecture", "Only macOS arm64 and x64 update channels are supported")
    normalized = _ARCHITECTURES[value.lower()]
    if architecture is None and normalized == "x64" and sys.platform == "darwin" and _is_rosetta():
        return "arm64"
    return normalized


def _scalar(value):
    # This is deliberately a small parser for the feed's scalar-only schema,
    # not a YAML loader. Tags, anchors, aliases and multiline content are refused.
    if not value:
        raise CatalogError("invalid_metadata", "The update feed contains an empty value")
    if value.startswith(("'", '"')):
        quote = value[0]
        if len(value) < 2 or value[-1] != quote or quote in value[1:-1] or "\\" in value[1:-1]:
            raise CatalogError("invalid_metadata", "Unsupported quoted value in update feed")
        return value[1:-1]
    if value[0] in "!&*|>{[" or " #" in value:
        raise CatalogError("invalid_metadata", "Unsupported YAML value in update feed")
    return value


def _parse_feed(text):
    """Parse the documented subset used by electron-updater's macOS feed."""
    fields = {}
    files = []
    in_files = False
    current = None
    for raw in text.splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        if "\t" in raw:
            raise CatalogError("invalid_metadata", "Tabs are not allowed in update metadata")
        top = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*):(?: (.*))?", raw)
        if top:
            key, value = top.groups()
            if key not in _TOP_KEYS or key in fields:
                raise CatalogError("invalid_metadata", "Unknown or duplicate field in update metadata")
            if key == "files":
                if value is not None:
                    raise CatalogError("invalid_metadata", "The update files field must be a list")
                fields[key] = files
                in_files = True
                current = None
            else:
                fields[key] = _scalar(value or "")
                in_files = False
            continue
        item = re.fullmatch(r"  - ([A-Za-z][A-Za-z0-9]*): (.+)", raw)
        member = re.fullmatch(r"    ([A-Za-z][A-Za-z0-9]*): (.+)", raw)
        if in_files and (item or member):
            key, value = (item or member).groups()
            if item:
                current = {}
                files.append(current)
            if current is None or key not in _FILE_KEYS or key in current:
                raise CatalogError("invalid_metadata", "Unknown or duplicate archive field in update metadata")
            current[key] = _scalar(value)
            continue
        raise CatalogError("invalid_metadata", "Unsupported structure in the official update feed")
    if "version" not in fields or not files:
        raise CatalogError("invalid_metadata", "The update feed is missing version or archive information")
    return fields


def _sha512(value):
    if not isinstance(value, str):
        raise CatalogError("invalid_metadata", "Archive SHA-512 is missing")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CatalogError("invalid_metadata", "Archive SHA-512 is not valid base64") from exc
    if len(decoded) != 64 or base64.b64encode(decoded).decode("ascii") != value:
        raise CatalogError("invalid_metadata", "Archive SHA-512 must encode exactly 64 bytes")
    return value


def _archive_url(value, version, architecture):
    if not isinstance(value, str) or len(value) > 2048:
        raise CatalogError("invalid_metadata", "Archive URL is missing or too long")
    try:
        url = urllib.parse.urlsplit(value)
        valid_origin = (url.scheme == "https" and url.hostname == "storage.googleapis.com"
                        and url.netloc == "storage.googleapis.com" and not url.username
                        and not url.password and not url.query and not url.fragment)
    except ValueError as exc:
        raise CatalogError("invalid_metadata", "Archive URL is malformed") from exc
    architecture_path = {"arm64": "darwin-arm", "x64": "darwin-x64"}[architecture]
    expected = (r"/antigravity-public/antigravity-hub/" + re.escape(version)
                + r"-[0-9]+/" + architecture_path + r"/Antigravity\.zip")
    if not valid_origin or re.fullmatch(expected, url.path) is None:
        raise CatalogError("invalid_metadata", "Archive URL must match the official host, version, and macOS architecture")
    return value


def validate_archive_url(url, version, architecture):
    """Return an eligible official ZIP URL, or raise CatalogError.

    Exposed so the installer can repeat the same URL/version/architecture check
    immediately before downloading. This function never makes a network call.
    """
    version_tuple(version)
    return _archive_url(url, version, normalize_architecture(architecture))


def _read_staging_uuid():
    """Read the existing official client's identifier locally; never create it."""
    path = Path.home() / "Library/Application Support/Antigravity/.updaterId"
    try:
        with path.open("rb") as reader:
            raw = reader.read(37)
        if len(raw) != 36:
            return None
        value = raw.decode("ascii")
        identifier = uuid.UUID(value)
        if (str(identifier) != value.lower() or identifier.variant != uuid.RFC_4122
                or identifier.version not in range(1, 9)):
            return None
        return identifier
    except (OSError, UnicodeError, ValueError):
        return None


def _check_staging(value):
    if value is None:
        return
    if re.fullmatch(r"(?:0|[1-9][0-9]?|100)", value) is None:
        raise CatalogError("invalid_metadata", "Rollout percentage must be an integer from 0 to 100")
    percentage = int(value)
    if percentage == 100:
        return
    identifier = _read_staging_uuid() if percentage else None
    # Match electron-updater's UUID final uint32 / 0xffffffff < percent / 100,
    # without exposing the identifier or sending it to the update server.
    if identifier is not None and int.from_bytes(identifier.bytes[-4:], "big") / 0xffffffff < percentage / 100:
        return
    raise CatalogError("staged_rollout", "The official update is not yet available to this client's rollout group")


def parse_manifest(data, architecture, *, feed_url=None):
    """Validate feed bytes and return a single eligible official macOS ZIP.

    This is public for offline validation. It does not establish that the
    archive's contents are compatible with the localization patch.
    A partial rollout reads only the installed client's existing local UUID;
    it does not generate or transmit an identifier.
    """
    architecture = normalize_architecture(architecture)
    expected_feed = FEED_BASE + "latest-" + architecture + "-mac.yml"
    if feed_url is not None and feed_url != expected_feed:
        raise CatalogError("invalid_metadata", "Unexpected official update feed URL")
    if not isinstance(data, bytes) or len(data) > MAX_FEED_BYTES:
        raise CatalogError("invalid_metadata", "Update metadata is not bytes or exceeds the size limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CatalogError("invalid_metadata", "Update metadata is not UTF-8") from exc
    fields = _parse_feed(text)
    version = fields["version"]
    try:
        version_tuple(version)
    except CatalogError as exc:
        raise CatalogError("invalid_metadata", "The update feed does not contain a stable x.y.z version") from exc
    # A channel must identify one archive unambiguously. Refuse a changed schema
    # rather than selecting the first item and possibly installing another build.
    if len(fields["files"]) != 1:
        raise CatalogError("invalid_metadata", "The update channel must contain exactly one archive")
    entry = fields["files"][0]
    url = _archive_url(entry.get("url"), version, architecture)
    sha512 = _sha512(entry.get("sha512"))
    size_value = entry.get("size", "")
    if not re.fullmatch(r"[1-9][0-9]{0,11}", size_value):
        raise CatalogError("invalid_metadata", "Archive size must be a positive integer")
    size = int(size_value)
    if size > 20 * 1024 ** 3:
        raise CatalogError("invalid_metadata", "Archive size exceeds the accepted limit")
    if "path" in fields and fields["path"] != "Antigravity.zip":
        raise CatalogError("invalid_metadata", "Top-level archive path does not match the ZIP")
    if "sha512" in fields and _sha512(fields["sha512"]) != sha512:
        raise CatalogError("invalid_metadata", "Top-level and archive SHA-512 values disagree")
    _check_staging(fields.get("stagingPercentage"))
    return {"version": version, "url": url, "sha512": sha512, "size": size,
            "architecture": architecture,
            "feed_url": expected_feed}


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CatalogError("invalid_metadata", "The official update feed redirected; refusing a different source")


def _fetch_once(request, timeout, max_bytes):
    """Fetch at most max_bytes over verified HTTPS; never follow redirects."""
    deadline = time.monotonic() + timeout
    # Python's verified TLS defaults stay enabled, including host-name checking.
    opener = urllib.request.build_opener(_NoRedirects(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(request, timeout=timeout) as response:
        if response.geturl() != request.full_url or response.status != 200:
            raise CatalogError("invalid_metadata", "Unexpected update feed response")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            if re.fullmatch(r"[0-9]{1,10}", content_length) is None or int(content_length) > max_bytes:
                raise CatalogError("invalid_metadata", "Update metadata exceeds the size limit")
        chunks = []
        length = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Official update feed timed out")
            # read1 returns one available chunk instead of waiting to fill a
            # large buffer while a server trickles data indefinitely.
            chunk = response.read1(min(4096, max_bytes + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > max_bytes:
                raise CatalogError("invalid_metadata", "Update metadata exceeds the size limit")
        if time.monotonic() >= deadline:
            raise TimeoutError("Official update feed timed out")
        if content_length is not None and length != int(content_length):
            raise CatalogError("invalid_metadata", "Update metadata was truncated")
        return b"".join(chunks)


def _fetch_bytes(request, timeout, max_bytes):
    """Retry a transient EOF/reset once for GET, within the original deadline."""
    deadline = time.monotonic() + timeout
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Official update feed timed out")
        try:
            return _fetch_once(request, remaining, max_bytes)
        except (urllib.error.URLError, OSError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            retryable = isinstance(reason, (ssl.SSLEOFError, ConnectionResetError))
            if attempt or request.get_method() != "GET" or not retryable:
                raise


def get_latest(architecture=None, *, timeout=10.0, installed_version=None, fetcher=None):
    """Fetch the offered official stable macOS build using exactly one feed URL.

    ``fetcher`` is an optional test transport accepting (Request, timeout,
    max_bytes) and returning bytes. Production callers should leave it unset.
    A production GET retries one transient TLS EOF/connection reset at most.
    ``installed_version`` only supplies the official x-app-version header;
    it is not used to hide older or equal channel versions.
    """
    architecture = normalize_architecture(architecture)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise CatalogError("invalid_timeout", "Timeout must be a finite number between 0 and 120 seconds")
    headers = {"User-Agent": USER_AGENT, "Accept": "text/yaml, text/plain;q=0.9"}
    if installed_version is not None:
        version_tuple(installed_version)
        headers["x-app-version"] = installed_version
    feed_url = FEED_BASE + "latest-" + architecture + "-mac.yml"
    request = urllib.request.Request(feed_url, headers=headers, method="GET")
    try:
        data = (fetcher or _fetch_bytes)(request, float(timeout), MAX_FEED_BYTES)
    except CatalogError:
        raise
    except urllib.error.HTTPError as exc:
        raise CatalogError("http", "Official update channel returned HTTP " + str(exc.code)) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise CatalogError("timeout", "Official update channel timed out") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise CatalogError("timeout", "Official update channel timed out") from exc
        raise CatalogError("network", "Could not reach the official update channel") from exc
    except OSError as exc:
        raise CatalogError("network", "Could not read the official update channel") from exc
    except http.client.HTTPException as exc:
        raise CatalogError("network", "The official update channel response was interrupted or malformed") from exc
    return parse_manifest(data, architecture, feed_url=feed_url)
