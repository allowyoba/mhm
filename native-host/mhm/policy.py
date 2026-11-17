"""Traffic budgets and per-profile routing overrides."""

from __future__ import annotations
import calendar
import copy
import datetime as dt
import hashlib
import json
import ipaddress
from .errors import HostError

DEFAULT_TRAFFIC_POLICY = {
    "enabled": False,
    "limitBytes": 50 * 1024**3,
    "period": "month",
    "cycleDay": 1,
    "action": "warn",
}


def normalize_traffic_policy(value):
    if not isinstance(value, dict):
        raise HostError("Traffic limit settings must be an object")
    result = {**DEFAULT_TRAFFIC_POLICY, **value}
    if not isinstance(result["enabled"], bool):
        raise HostError("Invalid traffic limit enabled value")
    try:
        result["limitBytes"] = int(result["limitBytes"])
        result["cycleDay"] = int(result["cycleDay"])
    except (TypeError, ValueError) as exc:
        raise HostError("Enter a valid traffic limit and cycle day") from exc
    if not 1 <= result["limitBytes"] <= 100 * 1024**4:
        raise HostError("Traffic limit must be between one byte and 100 TiB")
    if (
        not 1 <= result["cycleDay"] <= 31
        or result["period"] not in {"day", "month"}
        or result["action"] not in {"warn", "block"}
    ):
        raise HostError("Invalid traffic limit period or action")
    return {key: result[key] for key in DEFAULT_TRAFFIC_POLICY}


def traffic_window(policy, today=None):
    today = today or dt.datetime.now().astimezone().date()
    if policy["period"] == "day":
        return today, today + dt.timedelta(days=1)

    def month_date(year, month):
        return dt.date(
            year, month, min(policy["cycleDay"], calendar.monthrange(year, month)[1])
        )

    start = month_date(today.year, today.month)
    if today < start:
        previous = today.replace(day=1) - dt.timedelta(days=1)
        start = month_date(previous.year, previous.month)
    next_month = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return start, month_date(next_month.year, next_month.month)


def traffic_budget(store, policy, today=None):
    policy = normalize_traffic_policy(policy)
    start, end = traffic_window(policy, today)
    totals = store.period_totals(start.isoformat(), end.isoformat())
    used = totals["downloadBytes"] + totals["uploadBytes"]
    exceeded = policy["enabled"] and used >= policy["limitBytes"]
    return {
        **policy,
        "usedBytes": used,
        "remainingBytes": max(0, policy["limitBytes"] - used),
        "percent": round(used / policy["limitBytes"] * 100, 2),
        "warning": policy["enabled"] and used >= policy["limitBytes"] * 0.8,
        "exceeded": exceeded,
        "blocked": exceeded and policy["action"] == "block",
        "startDay": start.isoformat(),
        "endDay": end.isoformat(),
    }


def normalize_custom_rules(rules):
    if not isinstance(rules, list) or len(rules) > 2000:
        raise HostError("Use a list of up to 2000 custom rules")
    result = []
    for rule in rules:
        if (
            not isinstance(rule, str)
            or len(rule.encode("utf-8")) > 2048
            or "\n" in rule
            or "\r" in rule
        ):
            raise HostError("Each custom rule must be a single line up to 2048 bytes")
        rule = rule.strip()
        if rule and not rule.startswith("#"):
            if "," not in rule:
                raise HostError("A rule must include its type and target")
            result.append(rule)
    return result


def with_custom_rules(document, bundle):
    document = copy.deepcopy(document)
    rules = normalize_custom_rules(bundle.get("customRules", []))
    document["rules"] = rules + (document.get("rules") or [])
    return document


def profile_revision(bundle):
    text = bundle["yaml"]
    rules = bundle.get("customRules", [])
    if rules:
        text += "\0" + json.dumps(rules, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def normalize_excluded_subnets(value):
    if not isinstance(value, list) or len(value) > 256:
        raise HostError("Enter up to 256 IPv4 or IPv6 subnets")
    result = []
    for item in value:
        if not isinstance(item, str) or len(item) > 64 or "%" in item:
            raise HostError("Enter a valid IPv4 or IPv6 subnet")
        try:
            network = str(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError as exc:
            raise HostError(f"Invalid subnet: {item}") from exc
        if network not in result:
            result.append(network)
    return result


def with_subnet_exclusions(config, subnets):
    subnets = normalize_excluded_subnets(subnets)
    result = copy.deepcopy(config)
    if not subnets or result.get("mode", "rule") == "direct":
        return result
    rules = [
        f'{"IP-CIDR6" if ":" in subnet else "IP-CIDR"},{subnet},DIRECT'
        for subnet in subnets
    ]
    if result.get("mode") == "global":
        result["mode"] = "rule"
        result["rules"] = rules + ["MATCH,GLOBAL"]
    else:
        result["rules"] = rules + result.get("rules", [])
    return result
