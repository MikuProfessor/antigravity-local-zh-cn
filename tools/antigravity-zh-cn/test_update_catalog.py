"""Offline tests: no network, app mutation, or real archive downloads."""

import base64
import http.client
from pathlib import Path
import ssl
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import update_catalog as catalog


HASH = base64.b64encode(bytes(range(64))).decode("ascii")


def feed(version="2.32.2", arch="arm64", **overrides):
    platform = {"arm64": "darwin-arm", "x64": "darwin-x64"}[arch]
    values = {"version": version,
              "url": "https://storage.googleapis.com/antigravity-public/antigravity-hub/" + version + "-4665802025205760/" + platform + "/Antigravity.zip",
              "sha512": HASH, "size": "216300592", "top_hash": HASH}
    values.update(overrides)
    return ("version: {version}\nfiles:\n  - url: {url}\n    sha512: {sha512}\n    size: {size}\npath: Antigravity.zip\nsha512: {top_hash}\n".format(**values)).encode()


class VersionTests(unittest.TestCase):
    def test_numeric_comparison_and_first_use(self):
        self.assertEqual(catalog.version_tuple("2.29.0"), (2, 29, 0))
        self.assertEqual(catalog.compare_versions("2.9.0", "2.10.0"), "update_available")
        self.assertEqual(catalog.compare_versions("2.32.2", "2.32.2"), "up_to_date")
        self.assertEqual(catalog.compare_versions("2.33.0", "2.32.2"), "ahead")
        self.assertEqual(catalog.compare_versions(None, "2.32.2"), "not_installed")

    def test_rejects_nonstable_or_ambiguous_versions(self):
        for value in ["v2.29.0", "02.29.0", "2.029.0", "2.29", "2.29.0 ", "2.29.0\n", "2.29.0-beta.1", "2.29.0+build", "2.29.-1", "٢.٢٩.٠", None, True, 2290]:
            with self.subTest(value=value), self.assertRaises(catalog.CatalogError):
                catalog.version_tuple(value)

    def test_rosetta_automatic_detection_and_explicit_override(self):
        with patch.object(catalog.platform, "machine", return_value="x86_64"), patch.object(catalog.sys, "platform", "darwin"), patch.object(catalog, "_is_rosetta", return_value=True):
            self.assertEqual(catalog.normalize_architecture(), "arm64")
            self.assertEqual(catalog.normalize_architecture("x64"), "x64")
        with patch.object(catalog.platform, "machine", return_value="x86_64"), patch.object(catalog.sys, "platform", "darwin"), patch.object(catalog, "_is_rosetta", return_value=False):
            self.assertEqual(catalog.normalize_architecture(), "x64")


class ManifestTests(unittest.TestCase):
    def assert_invalid(self, data, arch="arm64"):
        with self.assertRaises(catalog.CatalogError) as raised:
            catalog.parse_manifest(data, arch)
        self.assertEqual(raised.exception.kind, "invalid_metadata")

    def test_actual_schema_and_architecture_aliases(self):
        for architecture, alias in [("arm64", "aarch64"), ("x64", "x86_64")]:
            with self.subTest(architecture=architecture):
                result = catalog.parse_manifest(feed(arch=architecture), alias)
                self.assertEqual(result["version"], "2.32.2")
                self.assertEqual(result["architecture"], architecture)
                self.assertEqual(result["sha512"], HASH)
                self.assertEqual(result["size"], 216300592)

    def test_rejects_mismatched_version_architecture_or_host(self):
        original = catalog.parse_manifest(feed(), "arm64")["url"]
        urls = [original.replace("2.32.2-", "2.33.0-"), original.replace("darwin-arm", "darwin-x64"),
                original.replace("https://", "http://"), original.replace("storage.googleapis.com", "storage.googleapis.com.evil.test"),
                original.replace("storage.googleapis.com", "user@storage.googleapis.com"),
                original.replace("storage.googleapis.com", "storage.googleapis.com:443"),
                original + "?token=anything", original + "#fragment", original.replace("Antigravity.zip", "../Antigravity.zip"),
                original.replace("antigravity-hub", "antigravity-ide"), original.replace("darwin-arm", "darwin-%61rm"),
                original.replace("Antigravity.zip", "Antigravity.dmg")]
        for value in urls:
            with self.subTest(url=value):
                self.assert_invalid(feed(url=value))

    def test_public_url_validator_matches_catalog(self):
        result = catalog.parse_manifest(feed(), "arm64")
        self.assertEqual(catalog.validate_archive_url(result["url"], "2.32.2", "aarch64"), result["url"])
        with self.assertRaises(catalog.CatalogError):
            catalog.validate_archive_url(result["url"], "2.33.0", "arm64")
        with self.assertRaises(catalog.CatalogError):
            catalog.parse_manifest(feed(), "arm64", feed_url="https://example.test/feed")

    def test_rejects_bad_hash_size_and_inconsistent_legacy_fields(self):
        for value in ["", "notbase64", base64.b64encode(b"short").decode(), HASH.rstrip("=")]:
            with self.subTest(hash=value):
                self.assert_invalid(feed(sha512=value))
        for value in ["0", "-1", "1.2", "yes", "0216300592", str(21 * 1024 ** 3)]:
            with self.subTest(size=value):
                self.assert_invalid(feed(size=value))
        self.assert_invalid(feed(top_hash=base64.b64encode(b"x" * 64).decode()))
        self.assert_invalid(feed().replace(b"path: Antigravity.zip", b"path: unrelated.zip"))

    def test_rejects_duplicate_or_changed_yaml_schema(self):
        cases = [feed() + b"version: 2.33.0\n", feed().replace(b"    size:", b"    sha512: " + HASH.encode() + b"\n    size:"),
                 feed().replace(b"files:\n", b"files: &alias\n"), feed().replace(b"  - url:", b"\t- url:"),
                 b"<html>not a feed</html>", b"version: 2.32.2\nfiles:\n", b"\xff", b"x" * (catalog.MAX_FEED_BYTES + 1),
                 feed().replace(b"version: 2.32.2", b"version: 2.32.2-beta.1"),
                 feed() + b"unexpectedField: ignored-no-more\n"]
        for data in cases:
            with self.subTest(data=data[:60]):
                self.assert_invalid(data)

    def test_rollout_matches_existing_client_uuid_at_exact_boundary(self):
        base = catalog.uuid.UUID("00000000-0000-4000-8000-000000000000").int
        for tail, offered in [(0, True), (0x33333332, True), (0x33333333, False), (0xffffffff, False)]:
            identifier = catalog.uuid.UUID(int=base | tail)
            with self.subTest(tail=tail), patch.object(catalog, "_read_staging_uuid", return_value=identifier):
                data = feed() + b"stagingPercentage: 20\n"
                if offered:
                    self.assertEqual(catalog.parse_manifest(data, "arm64")["version"], "2.32.2")
                else:
                    with self.assertRaises(catalog.CatalogError) as raised:
                        catalog.parse_manifest(data, "arm64")
                    self.assertEqual(raised.exception.kind, "staged_rollout")

    def test_rollout_zero_full_missing_and_invalid_percentages(self):
        with patch.object(catalog, "_read_staging_uuid", side_effect=AssertionError("No identifier needed")):
            for suffix in (b"", b"stagingPercentage: 100\n"):
                catalog.parse_manifest(feed() + suffix, "arm64")
            with self.assertRaises(catalog.CatalogError) as raised:
                catalog.parse_manifest(feed() + b"stagingPercentage: 0\n", "arm64")
            self.assertEqual(raised.exception.kind, "staged_rollout")
            for value in ("-1", "101", "1.5", "00", "01", "nan", "inf", "true", "1e2", ""):
                with self.subTest(value=value):
                    self.assert_invalid(feed() + ("stagingPercentage: " + value + "\n").encode())
        with patch.object(catalog, "_read_staging_uuid", return_value=None):
            with self.assertRaises(catalog.CatalogError) as raised:
                catalog.parse_manifest(feed() + b"stagingPercentage: 50\n", "arm64")
            self.assertEqual(raised.exception.kind, "staged_rollout")

    def test_staging_uuid_reader_is_bounded_read_only_and_strict(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(catalog.Path, "home", return_value=Path(folder)):
            path = Path(folder) / "Library/Application Support/Antigravity/.updaterId"
            self.assertIsNone(catalog._read_staging_uuid())
            self.assertFalse(path.parent.exists())
            path.parent.mkdir(parents=True)
            valid = b"00000000-0000-4000-8000-000000000000"
            for data in (b"invalid", valid + b"\n", valid * 2000, b"0" * 36, b"\xff" * 36):
                path.write_bytes(data)
                self.assertIsNone(catalog._read_staging_uuid())
                self.assertEqual(path.read_bytes(), data)
            path.write_bytes(valid)
            self.assertEqual(str(catalog._read_staging_uuid()), valid.decode())


class FetchTests(unittest.TestCase):
    def response(self, data, length=None):
        class Response:
            status = 200
            def __init__(self):
                self.data = data
                self.headers = {} if length is None else {"Content-Length": length}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def geturl(self):
                return catalog.FEED_BASE + "latest-arm64-mac.yml"
            def read1(self, size):
                result, self.data = self.data[:size], self.data[size:]
                return result
        return Response()

    def test_transport_bounds_and_truncation(self):
        responses = [self.response(b"partial", "100"),
                     self.response(b"", str(catalog.MAX_FEED_BYTES + 1)),
                     self.response(b"x" * (catalog.MAX_FEED_BYTES + 1)),
                     self.response(b"", "9" * 5000)]
        for response in responses:
            with self.subTest(length=response.headers), patch.object(catalog.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises(catalog.CatalogError) as raised:
                    catalog.get_latest("arm64")
                self.assertEqual(raised.exception.kind, "invalid_metadata")

    def test_transport_total_deadline(self):
        with patch.object(catalog.urllib.request, "build_opener") as opener, patch.object(catalog.time, "monotonic", side_effect=[0, 0, 0, 1, 11]):
            opener.return_value.open.return_value = self.response(feed())
            with self.assertRaises(catalog.CatalogError) as raised:
                catalog.get_latest("arm64", timeout=10)
            self.assertEqual(raised.exception.kind, "timeout")

    def test_production_get_retries_only_transient_eof_or_reset_once(self):
        for error in (ssl.SSLEOFError(8, "synthetic EOF"), ConnectionResetError(54, "synthetic reset")):
            for wrapped in (error, urllib.error.URLError(error)):
                with self.subTest(type=type(error), wrapped=isinstance(wrapped, urllib.error.URLError)), patch.object(catalog, "_fetch_once", side_effect=[wrapped, feed()]) as transport, patch.object(catalog.time, "monotonic", side_effect=[100, 101, 104]):
                    self.assertEqual(catalog.get_latest("arm64", timeout=10)["version"], "2.32.2")
                    self.assertEqual(transport.call_count, 2)
                    first, second = transport.call_args_list
                    self.assertIs(first.args[0], second.args[0])
                    self.assertEqual(first.args[0].get_method(), "GET")
                    self.assertEqual([first.args[1], second.args[1]], [9.0, 6.0])
                    self.assertEqual(first.args[2], catalog.MAX_FEED_BYTES)

    def test_retry_is_bounded_and_cannot_extend_timeout(self):
        error = urllib.error.URLError(ssl.SSLEOFError(8, "synthetic EOF"))
        with patch.object(catalog, "_fetch_once", side_effect=error) as transport:
            with self.assertRaises(catalog.CatalogError) as raised:
                catalog.get_latest("arm64")
            self.assertEqual(raised.exception.kind, "network")
            self.assertEqual(transport.call_count, 2)
        with patch.object(catalog, "_fetch_once", side_effect=error) as transport, patch.object(catalog.time, "monotonic", side_effect=[0, 0, 10]):
            with self.assertRaises(catalog.CatalogError) as raised:
                catalog.get_latest("arm64", timeout=10)
            self.assertEqual(raised.exception.kind, "timeout")
            self.assertEqual(transport.call_count, 1)

    def test_normal_success_certificates_http_and_format_errors_do_not_retry(self):
        with patch.object(catalog, "_fetch_once", return_value=feed()) as transport:
            catalog.get_latest("arm64")
            self.assertEqual(transport.call_count, 1)
        errors = (ssl.SSLCertVerificationError(1, "synthetic certificate failure"),
                  urllib.error.URLError(ssl.SSLCertVerificationError(1, "synthetic certificate failure")),
                  ssl.SSLError(1, "synthetic nontransient TLS failure"),
                  urllib.error.HTTPError(catalog.FEED_BASE, 503, "unavailable", {}, None),
                  catalog.CatalogError("invalid_metadata", "synthetic malformed reply"))
        for error in errors:
            with self.subTest(type=type(error)), patch.object(catalog, "_fetch_once", side_effect=error) as transport:
                with self.assertRaises(catalog.CatalogError):
                    catalog.get_latest("arm64")
                self.assertEqual(transport.call_count, 1)
        with patch.object(catalog, "_fetch_once", return_value=b"not a manifest") as transport:
            with self.assertRaises(catalog.CatalogError) as raised:
                catalog.get_latest("arm64")
            self.assertEqual(raised.exception.kind, "invalid_metadata")
            self.assertEqual(transport.call_count, 1)

    def test_one_official_request_with_version_header(self):
        calls = []
        def transport(request, timeout, limit):
            calls.append((request, timeout, limit))
            return feed()
        result = catalog.get_latest("arm64", timeout=3, installed_version="2.29.0", fetcher=transport)
        self.assertEqual(len(calls), 1)
        request, timeout, limit = calls[0]
        self.assertEqual(request.full_url, catalog.FEED_BASE + "latest-arm64-mac.yml")
        self.assertEqual(request.get_header("X-app-version"), "2.29.0")
        self.assertEqual((timeout, limit), (3.0, catalog.MAX_FEED_BYTES))
        self.assertEqual(result["feed_url"], request.full_url)

    def test_network_errors_never_become_up_to_date(self):
        failures = [(TimeoutError(), "timeout"), (urllib.error.URLError(TimeoutError()), "timeout"),
                    (urllib.error.URLError("offline"), "network"), (OSError("offline"), "network"),
                    (http.client.IncompleteRead(b"partial"), "network"),
                    (urllib.error.HTTPError(catalog.FEED_BASE, 503, "unavailable", {}, None), "http")]
        for error, kind in failures:
            def transport(*args):
                raise error
            with self.subTest(kind=kind), self.assertRaises(catalog.CatalogError) as raised:
                catalog.get_latest("arm64", fetcher=transport)
            self.assertEqual(raised.exception.kind, kind)

    def test_bad_inputs_do_not_make_requests(self):
        def forbidden(*args):
            self.fail("Transport must not run for invalid arguments")
        for value in [0, -1, 121, float("inf"), float("nan"), True, "10"]:
            with self.subTest(timeout=value), self.assertRaises(catalog.CatalogError):
                catalog.get_latest("arm64", timeout=value, fetcher=forbidden)
        for value in ["arm", "universal", "arm64 ", "", 64]:
            with self.subTest(architecture=value), self.assertRaises(catalog.CatalogError):
                catalog.get_latest(value, fetcher=forbidden)
        with self.assertRaises(catalog.CatalogError):
            catalog.get_latest("arm64", installed_version="2.29.0-beta", fetcher=forbidden)

    def test_redirect_is_rejected_before_following(self):
        handler = catalog._NoRedirects()
        with self.assertRaises(catalog.CatalogError) as raised:
            handler.redirect_request(None, None, 302, "Found", {}, "http://untrusted.test/feed")
        self.assertEqual(raised.exception.kind, "invalid_metadata")


if __name__ == "__main__":
    unittest.main()
