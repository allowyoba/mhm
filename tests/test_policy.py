"""Traffic budgets and custom-rule lifecycle regressions."""

import datetime as dt
import time
import unittest
from unittest import mock
import test_native_host as fixtures

nh = fixtures.nh
YAML = fixtures.YAML
from mhm.policy import (
    normalize_traffic_policy,
    traffic_window,
    traffic_budget,
    with_custom_rules,
)


class PolicyTests(fixtures.HostTest):
    def test_billing_day_handles_short_months(self):
        policy = normalize_traffic_policy({"cycleDay": 31})
        self.assertEqual(
            traffic_window(policy, dt.date(2026, 2, 27)),
            (dt.date(2026, 1, 31), dt.date(2026, 2, 28)),
        )
        self.assertEqual(
            traffic_window(policy, dt.date(2026, 2, 28)),
            (dt.date(2026, 2, 28), dt.date(2026, 3, 31)),
        )

    def test_only_current_period_counts_toward_limit(self):
        nh.TRAFFIC.record_delta(500, 0, dt.datetime(2026, 1, 30, 12).timestamp())
        nh.TRAFFIC.record_delta(60, 25, dt.datetime(2026, 2, 10, 12).timestamp())
        policy = normalize_traffic_policy(
            {
                "enabled": True,
                "limitBytes": 100,
                "period": "month",
                "cycleDay": 1,
                "action": "block",
            }
        )
        budget = traffic_budget(nh.TRAFFIC, policy, dt.date(2026, 2, 15))
        self.assertEqual(budget["usedBytes"], 85)
        self.assertTrue(budget["warning"])
        self.assertFalse(budget["blocked"])
        nh.TRAFFIC.record_delta(15, 0, dt.datetime(2026, 2, 11, 12).timestamp())
        self.assertTrue(
            traffic_budget(nh.TRAFFIC, policy, dt.date(2026, 2, 15))["blocked"]
        )

    def test_invalid_limit_does_not_overwrite_saved_settings(self):
        nh.SETTINGS.save({"trafficPolicy": {"enabled": True, "limitBytes": 100}})
        with self.assertRaises(nh.HostError):
            nh.handle({"command": "saveTrafficPolicy", "policy": {"limitBytes": 0}})
        self.assertEqual(nh.SETTINGS.load()["trafficPolicy"]["limitBytes"], 100)

    def test_rules_survive_subscription_updates_and_change_revision(self):
        with mock.patch.object(
            nh, "fetch_mihomo_subscription", return_value=(YAML, None)
        ):
            profile = self.save(
                source="subscription", url="https://example.org/sub", yaml=None
            )
        result = nh.handle(
            {
                "command": "saveProfileRules",
                "profileId": profile["id"],
                "rules": ["DOMAIN-SUFFIX,example.org,DIRECT"],
            }
        )
        self.assertNotEqual(result["profile"]["revision"], profile["revision"])
        with mock.patch.object(
            nh,
            "fetch_mihomo_subscription",
            return_value=(YAML.replace("First", "Third"), None),
        ):
            nh.PROFILES.update(profile["id"])
        bundle = nh.PROFILES.bundle(profile["id"])
        config = with_custom_rules(nh.profile_document(bundle["yaml"]), bundle)
        self.assertEqual(config["rules"][0], "DOMAIN-SUFFIX,example.org,DIRECT")
        self.assertEqual(config["rules"][-1], "MATCH,Choose")

    def test_failed_rule_validation_keeps_previous_revision(self):
        profile = self.save()
        with mock.patch.object(
            nh, "validate_mihomo", side_effect=nh.HostError("invalid")
        ):
            with self.assertRaises(nh.HostError):
                nh.handle(
                    {
                        "command": "saveProfileRules",
                        "profileId": profile["id"],
                        "rules": ["DOMAIN,example.org,Missing"],
                    }
                )
        self.assertEqual(
            nh.PROFILES.get_meta(profile["id"])["revision"], profile["revision"]
        )
        self.assertNotIn("customRules", nh.PROFILES.bundle(profile["id"]))

    def test_no_pending_update_for_unchanged_custom_rules(self):
        profile = self.save()
        first = nh.handle(
            {
                "command": "saveProfileRules",
                "profileId": profile["id"],
                "rules": ["DOMAIN,example.org,DIRECT"],
            }
        )["profile"]
        second = nh.handle(
            {
                "command": "saveProfileRules",
                "profileId": profile["id"],
                "rules": ["DOMAIN,example.org,DIRECT"],
            }
        )["profile"]
        self.assertEqual(first["revision"], second["revision"])


@unittest.skipUnless(
    __import__("test_native_host").MIHOMO_BINARY.is_file(), "Verified engine required"
)
class RealBudgetTests(fixtures.HostTest):
    def setUp(self):
        super().setUp()
        self.validation.stop()
        self.binary = mock.patch.object(
            nh, "find_mihomo", return_value=str(fixtures.MIHOMO_BINARY)
        )
        self.binary.start()

    def tearDown(self):
        self.binary.stop()
        super().tearDown()

    def test_limit_rejects_new_connections_and_works_without_popup(self):
        profile = self.save()
        nh.handle({"command": "connect", "profileId": profile["id"]})
        nh.SETTINGS.save(
            {
                "trafficPolicy": {
                    "enabled": True,
                    "limitBytes": 10,
                    "period": "day",
                    "action": "block",
                }
            }
        )
        nh.TRAFFIC.record_delta(11, 0)
        deadline = time.monotonic() + 4
        while nh.CLIENT.running() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(nh.CLIENT.running())
        self.assertEqual(nh.CLIENT.status()["stopReason"], "traffic-limit")
        with self.assertRaises(nh.HostError) as caught:
            nh.handle({"command": "connect", "profileId": profile["id"]})
        self.assertEqual(caught.exception.code, "TRAFFIC_LIMIT")
        nh.SETTINGS.save({"trafficPolicy": {"enabled": False}})
        self.assertEqual(
            nh.handle({"command": "connect", "profileId": profile["id"]})["status"],
            "connected",
        )
