import copy
import http.client
import http.server
from pathlib import Path
import threading
import unittest
import test_native_host as fixtures
from mhm.policy import normalize_excluded_subnets, with_subnet_exclusions

nh = fixtures.nh


class SubnetTests(fixtures.HostTest):
    def test_normalizes_both_families_and_rejects_invalid_settings_atomically(self):
        settings = nh.handle(
            {
                "command": "saveSettings",
                "excludedSubnets": [
                    "192.168.8.12/24",
                    "fd00:0:0::1/8",
                    "192.168.8.0/24",
                ],
            }
        )["settings"]
        self.assertEqual(settings["excludedSubnets"], ["192.168.8.0/24", "fd00::/8"])
        before = nh.SETTINGS_PATH.read_bytes()
        for value in [
            ["not-a-network"],
            ["10.0.0.0/33"],
            ["::/129"],
            ["fe80::%en0/64"],
            "10.0.0.0/8",
        ]:
            with self.subTest(value=value), self.assertRaises(nh.HostError):
                nh.handle({"command": "saveSettings", "excludedSubnets": value})
            self.assertEqual(nh.SETTINGS_PATH.read_bytes(), before)

    def test_precedes_reject_and_keeps_global_routing_after_exclusions(self):
        config = {"mode": "rule", "rules": ["MATCH,REJECT"]}
        before = copy.deepcopy(config)
        result = with_subnet_exclusions(config, ["10.0.0.0/8", "fd00::/8"])
        self.assertEqual(
            result["rules"],
            ["IP-CIDR,10.0.0.0/8,DIRECT", "IP-CIDR6,fd00::/8,DIRECT", "MATCH,REJECT"],
        )
        self.assertEqual(config, before)
        result = with_subnet_exclusions({**config, "mode": "global"}, ["10.0.0.0/8"])
        self.assertEqual(result["mode"], "rule")
        self.assertEqual(result["rules"][-1], "MATCH,GLOBAL")
        self.assertEqual(
            with_subnet_exclusions({**config, "mode": "direct"}, ["10.0.0.0/8"])[
                "mode"
            ],
            "direct",
        )

    @unittest.skipUnless(fixtures.MIHOMO_BINARY.exists(), "mihomo required")
    def test_real_core_bypasses_reject_in_rule_and_global_modes_and_reapplies_changes(
        self,
    ):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"direct subnet")

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            nh.SETTINGS.save(
                {
                    "mihomoPath": str(fixtures.MIHOMO_BINARY),
                    "excludedSubnets": ["127.0.0.1/32", "::1/128"],
                }
            )
            profile = self.save(
                yaml='proxies:\n- name: Blocked\n  type: socks5\n  server: 127.0.0.1\n  port: 65000\nrules: ["MATCH,REJECT"]\n'
            )
            nh.CLIENT.connect(profile["id"])

            def fetch():
                client = http.client.HTTPConnection(
                    "127.0.0.1", nh.CLIENT.local_port, timeout=5
                )
                try:
                    client.request("GET", f"http://127.0.0.1:{server.server_port}/")
                    response = client.getresponse()
                    return response.status, response.read()
                finally:
                    client.close()

            self.assertEqual(fetch(), (200, b"direct subnet"))
            old_pid = nh.CLIENT.process.pid
            nh.handle({"command": "setMode", "mode": "global"})
            self.assertEqual(nh.CLIENT.status()["mode"], "global")
            self.assertNotEqual(old_pid, nh.CLIENT.process.pid)
            nh.CLIENT.api("PUT", "/proxies/GLOBAL", {"name": "Blocked"})
            self.assertEqual(fetch(), (200, b"direct subnet"))
            self.assertEqual(nh.CLIENT.api("GET", "/configs")["mode"], "rule")
            nh.handle({"command": "setMode", "mode": "rule"})
            nh.SETTINGS.save({"excludedSubnets": []})
            old_pid = nh.CLIENT.process.pid
            nh.CLIENT.connect(profile["id"])
            self.assertNotEqual(old_pid, nh.CLIENT.process.pid)
            try:
                self.assertNotEqual(fetch()[0], 200)
            except (OSError, http.client.HTTPException):
                pass
        finally:
            nh.CLIENT.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
