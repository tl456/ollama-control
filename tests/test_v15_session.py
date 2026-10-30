"""Synthetic optional-password UI auth; no model or managed-engine startup."""
import asyncio
from contextlib import contextmanager
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient
from test_core import ROOT, SOURCE_ROOT
from app import config, lifecycle, network, server, tray


class UiPasswordPolicyTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(ROOT.resolve(), SOURCE_ROOT.resolve())
        self.assertTrue(ROOT.name.startswith("local-ai-test-"))
        lifecycle.reset_managed()
        config.write_json(ROOT / "config/settings.json", {**config.DEFAULTS, "gpu_monitor": False})
        self.client = TestClient(server.app)  # No lifespan or Ollama start.
        self.addCleanup(self.client.close)

    def login_required(self, response):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "请先登录", "code": "login_required"})

    def password(self):
        network.set_password("SYNTHETIC-password-905")

    def login(self, remember=False):
        response = self.client.post("/api/auth/login", json={"password": "SYNTHETIC-password-905", "remember": remember})
        self.assertEqual(response.status_code, 200)
        return response

    def test_no_password_ui_works_without_temporary_token(self):
        self.assertFalse(hasattr(server, "TOKEN"))
        self.assertNotIn("token", self.client.get("/api/bootstrap").json())
        with patch.object(server.store, "create", return_value={"id": "synthetic"}) as create:
            self.assertEqual(self.client.post("/api/conversations").status_code, 200)
            create.assert_called_once()

    def test_password_local_api_requires_cookie_before_handler(self):
        self.password()
        with patch.object(server.store, "create") as create:
            for headers in ({}, {"X-Local-Control": server.CONTROL_TOKEN}):
                self.login_required(self.client.post("/api/conversations", headers=headers))
            create.assert_not_called()
        self.login()
        with patch.object(server.store, "create", return_value={"id": "logged-in-once"}) as create:
            response = self.client.post("/api/conversations")
            self.assertEqual(response.status_code, 200)
            create.assert_called_once()

    def test_public_shell_and_bootstrap_do_not_expose_config_or_control(self):
        self.password()
        config.write_json(ROOT / "config/secrets.json", {"synthetic": "SYNTHETIC_CLOUD_SECRET_905"})
        self.assertEqual(self.client.get("/").status_code, 200)
        for headers in ({}, {"X-Local-Control": server.CONTROL_TOKEN}):
            response = self.client.get("/api/bootstrap", headers=headers)
            self.assertEqual(response.status_code, 200)
            value = response.json()
            self.assertEqual(set(value), {"version", "authenticated", "can_manage", "login_required", "auth"})
            self.assertTrue(value["login_required"])
            self.assertFalse(value["can_manage"])
            for secret in (server.CONTROL_TOKEN, "SYNTHETIC_CLOUD_SECRET_905"):
                self.assertNotIn(secret, response.text)
            self.login_required(self.client.get("/api/settings", headers=headers))
        self.login()
        full = self.client.get("/api/bootstrap").json()
        self.assertIn("settings", full)
        self.assertNotIn("token", full)
        self.assertNotIn(server.CONTROL_TOKEN, json.dumps(full))

    def test_owner_control_only_exact_post_lifecycle_not_prefix_or_get(self):
        self.password()
        headers = {"X-Local-Control": server.CONTROL_TOKEN}
        with patch.object(server, "write_json") as write:
            response = self.client.post("/api/ui/show", headers=headers)
            self.assertEqual(response.status_code, 200)
            write.assert_called_once()
            for method, path in (("GET", "/api/ui/show"), ("POST", "/api/ui/show/extra"),
                                 ("POST", "/api/ui/pick-directory"), ("GET", "/api/settings"),
                                 ("GET", "/api/memory"), ("GET", "/api/python/runs")):
                self.login_required(self.client.request(method, path, headers=headers))
            write.assert_called_once()
        with patch.object(server.turn_queue, "stop_all") as stop, patch.object(server.python_lab, "stop"):
            self.assertEqual(self.client.post("/api/ui/stop-all", headers=headers).status_code, 200)
            stop.assert_called_once()
        with patch.object(server.providers, "local", return_value={"models": []}):
            self.assertEqual(self.client.post("/api/ui/unload-all", headers=headers).status_code, 200)
        # The route is reached but this synthetic TestClient has no shutdown hook.
        self.assertEqual(self.client.post("/api/ui/quit", headers=headers).status_code, 409)

    def test_bad_origin_host_or_wrong_control_cannot_bypass_password(self):
        self.password()
        with patch.object(server, "write_json") as write:
            for headers in ({"X-Local-Control": "wrong"}, {}):
                self.login_required(self.client.post("/api/ui/show", headers=headers))
            for url, headers in (("/api/ui/show", {"Origin": "https://other.invalid", "X-Local-Control": server.CONTROL_TOKEN}),
                                 ("http://other.invalid/api/ui/show", {"X-Local-Control": server.CONTROL_TOKEN})):
                response = self.client.post(url, headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("ui_session_expired", response.text)
            write.assert_not_called()

    def remote_client(self, full=False):
        config.write_json(ROOT / "config/settings.json", {**config.settings(), "web_bind": "0.0.0.0", "remote_full_access": full})
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app, client=("192.0.2.73", 8041)),
                                 base_url="http://192.0.2.1:8765")

    def test_remote_cookie_and_optional_full_access_still_require_owner_grant(self):
        async def exercise():
            async with self.remote_client() as client:
                self.assertEqual((await client.get("/api/conversations")).status_code, 200)
                self.assertEqual((await client.get("/api/settings")).status_code, 403)
            async with self.remote_client(full=True) as client:
                self.assertEqual((await client.get("/api/settings")).status_code, 200)
                response = await client.put("/api/settings", json={"remote_full_access": False})
                self.assertEqual(response.status_code, 400)
                self.assertIn("只可在宿主机", response.json()["detail"])
                self.assertEqual((await client.post("/api/auth/password", json={"password": "SYNTHETIC-new-905"})).status_code, 403)
            self.password()
            async with self.remote_client(full=True) as client:
                self.login_required(await client.get("/api/settings"))
                self.login_required(await client.post("/api/ui/show", headers={"X-Local-Control": server.CONTROL_TOKEN}))
                self.assertEqual((await client.post("/api/auth/login", json={"password": "SYNTHETIC-password-905"})).status_code, 200)
                self.assertEqual((await client.get("/api/settings")).status_code, 200)
        asyncio.run(exercise())

    def test_local_owner_can_enable_full_access_without_password(self):
        response = self.client.put("/api/settings", json={"remote_full_access": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["remote_full_access"])

    def test_logout_and_password_change_revoke_cookie(self):
        self.password()
        response = self.login(remember=True)
        self.assertIn("Max-Age=2592000", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/settings").status_code, 200)
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.login_required(self.client.get("/api/settings"))
        self.login()
        self.assertEqual(self.client.post("/api/auth/password", json={"password": "SYNTHETIC-new-905"}).status_code, 200)
        self.login_required(self.client.get("/api/settings"))

    def test_owner_tray_commands_use_only_internal_control_header(self):
        response = Mock()
        with patch.object(tray, "read_json", return_value={"token": "SYNTHETIC_OWNER"}), patch.object(
                tray.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertTrue(tray.notify_existing("http://127.0.0.1:8765"))
            request = urlopen.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/ui/show")
            self.assertEqual(request.get_header("X-local-control"), "SYNTHETIC_OWNER")
            self.assertEqual({key.lower() for key in request.headers}, {"content-type", "x-local-control"})
            urlopen.reset_mock()
            tray.Tray("http://127.0.0.1:8765", lambda: None, lambda: None).request("/api/ui/stop-all")
            self.assertEqual(urlopen.call_count, 1)
            self.assertTrue(urlopen.call_args.args[0].full_url.endswith("/api/ui/stop-all"))

    @contextmanager
    def http_backend(self):
        # A real disposable HTTP process; application lifespan is OFF, so no
        # Ollama, scheduler, hardware probe or managed service is started.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        code = (
            "import os,sys,threading,uvicorn\nfrom app.server import app\n"
            "s=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=int(os.environ['SYNTHETIC_HTTP_PORT']),"
            "lifespan='off',access_log=False,log_level='error'))\n"
            "def done():\n sys.stdin.readline();s.should_exit=True\n"
            "threading.Thread(target=done,daemon=True).start()\n"
            "timer=threading.Timer(40,lambda:setattr(s,'should_exit',True));timer.daemon=True;timer.start()\ns.run()\n"
        )
        env = {**os.environ, "LOCAL_AI_ROOT": str(ROOT), "LOCAL_AI_DISABLE_GPU_PROBE": "1",
               "SYNTHETIC_HTTP_PORT": str(port), "PYTHONUTF8": "1"}
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code], cwd=SOURCE_ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            base = "http://127.0.0.1:" + str(port)
            until = time.monotonic() + 20
            while time.monotonic() < until:
                if process.poll() is not None:
                    raise AssertionError("Synthetic HTTP backend exited before ready")
                try:
                    if httpx.get(base + "/health", trust_env=False, timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.05)
            else:
                raise AssertionError("Synthetic HTTP backend not ready")
            yield base
        finally:
            if process.poll() is None:
                process.stdin.write(b"STOP\n")
                process.stdin.flush()
            output, _ = process.communicate(timeout=45)
            self.assertEqual(process.returncode, 0, output.decode("utf-8", errors="replace")[-1000:])

    def test_real_http_cookie_survives_backend_process_restart(self):
        self.password()
        with self.http_backend() as base:
            with httpx.Client(base_url=base, trust_env=False) as client:
                self.login_required(client.get("/api/settings"))
                self.assertEqual(client.post("/api/auth/login", json={"password": "SYNTHETIC-password-905", "remember": True}).status_code, 200)
                cookies = httpx.Cookies(client.cookies)
                self.assertNotIn("token", client.get("/api/bootstrap").json())
                self.assertEqual(client.get("/api/settings").status_code, 200)
        with self.http_backend() as base:
            with httpx.Client(base_url=base, trust_env=False, cookies=cookies) as client:
                self.assertEqual(client.get("/api/settings").status_code, 200)
                boot = client.get("/api/bootstrap").json()
                self.assertNotIn("token", boot)
                self.assertTrue(boot["auth"]["authenticated"])


if __name__ == "__main__":
    unittest.main()
