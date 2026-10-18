"""Offline archive download integrity tests; no GitHub requests or real tokens."""
import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("download_job", Path(__file__).with_name("job.py"))
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)


class DownloadIntegrityTests(unittest.TestCase):
    def fetch(self, directory, response):
        artifact = {"name": "results", "archive_download_url": "https://example.invalid/archive",
                    "size_in_bytes": 9}
        with mock.patch.object(job, "gh_api", return_value={"artifacts": [artifact]}), \
             mock.patch.object(job, "gh_token", return_value="offline-fixture"), \
             mock.patch.object(job.urllib.request, "urlopen", return_value=response), \
             contextlib.redirect_stdout(io.StringIO()):
            job.actions_fetch(123, str(directory))

    def test_interrupted_read_preserves_existing_archive(self):
        for existing in (None, b"previous validated archive"):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result = root / "result"
                result.mkdir()
                dest = result / "results.zip"
                if existing is not None:
                    dest.write_bytes(existing)
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.read.side_effect = http.client.IncompleteRead(b"partial", 100)
                with self.assertRaises(http.client.IncompleteRead):
                    self.fetch(root, response)
                self.assertEqual(dest.read_bytes() if dest.exists() else None, existing)
                self.assertEqual(sorted(p.name for p in result.iterdir()),
                                 [] if existing is None else ["results.zip"])

    def test_success_replaces_archive_only_after_response_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = root / "result"
            result.mkdir()
            dest = result / "results.zip"
            dest.write_bytes(b"old")
            observed = []

            class Response(io.BytesIO):
                def read(self, *args):
                    observed.append(dest.read_bytes())
                    return super().read(*args)

            self.fetch(root, Response(b"new complete archive"))
            self.assertTrue(observed)
            self.assertTrue(all(data == b"old" for data in observed))
            self.assertEqual(dest.read_bytes(), b"new complete archive")
            self.assertEqual([p.name for p in result.iterdir()], ["results.zip"])

    def test_publish_failure_keeps_previous_archive_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = root / "result"
            result.mkdir()
            dest = result / "results.zip"
            dest.write_bytes(b"old")
            with mock.patch.object(job.os, "replace", side_effect=OSError("fixture publish failure")):
                with self.assertRaisesRegex(OSError, "fixture publish failure"):
                    self.fetch(root, io.BytesIO(b"new"))
            self.assertEqual(dest.read_bytes(), b"old")
            self.assertEqual([p.name for p in result.iterdir()], ["results.zip"])

    def test_success_creates_new_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fetch(root, io.BytesIO(b"complete archive"))
            self.assertEqual((root / "result/results.zip").read_bytes(), b"complete archive")
            self.assertEqual([p.name for p in (root / "result").iterdir()], ["results.zip"])


class DownloadRedirectTests(unittest.TestCase):
    def fetch_archive(self, redirects):
        observed = []
        routes = {}
        archive = b"complete fixture archive"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append((self.path, self.headers.get("Authorization")))
                target = routes.get(self.path)
                self.send_response(302 if target else 200)
                if target:
                    self.send_header("Location", target)
                self.end_headers()
                if not target:
                    self.wfile.write(archive)

            def log_message(self, *args):
                pass

        servers = [HTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
        origin = "http://127.0.0.1:{}".format(servers[0].server_port)
        storage = "http://127.0.0.1:{}".format(servers[1].server_port)
        if redirects:
            routes["/archive"] = storage + "/blob"
        if redirects == 2:
            routes["/archive"] = origin + "/intermediate"
            routes["/intermediate"] = storage + "/blob"
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
        for thread in threads:
            thread.start()
        try:
            # Exercise urllib's real redirect handling, without environment
            # proxies, live GitHub requests, or reading the user's credentials.
            opener = job.urllib.request.build_opener(job.urllib.request.ProxyHandler({}))
            artifact = {"name": "results", "archive_download_url": origin + "/archive",
                        "size_in_bytes": len(archive)}
            with tempfile.TemporaryDirectory() as tmp, \
                 mock.patch.object(job, "gh_api", return_value={"artifacts": [artifact]}), \
                 mock.patch.object(job, "gh_token", return_value="offline-fixture"), \
                 mock.patch.object(job.urllib.request, "urlopen",
                                   side_effect=lambda req, timeout: opener.open(req, timeout=3)), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(job.actions_fetch(123, tmp), 0)
                self.assertEqual((Path(tmp) / "result/results.zip").read_bytes(), archive)
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=3)
        return observed

    def test_direct_download_keeps_initial_authentication(self):
        self.assertEqual(self.fetch_archive(0), [("/archive", "token offline-fixture")])

    def test_cross_origin_redirect_does_not_forward_authentication(self):
        self.assertEqual(self.fetch_archive(1),
                         [("/archive", "token offline-fixture"), ("/blob", None)])

    def test_redirect_chain_does_not_restore_authentication(self):
        self.assertEqual(self.fetch_archive(2),
                         [("/archive", "token offline-fixture"),
                          ("/intermediate", None), ("/blob", None)])


if __name__ == "__main__":
    unittest.main()
