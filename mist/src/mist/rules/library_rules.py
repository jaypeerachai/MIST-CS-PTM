"""Load the supported library rules used by mock checks."""

import json
from pathlib import Path

from mist.resources import DATA_DIR


class LibraryRules:
    def __init__(self, data):
        self.service_clients = data["service_clients"]
        self.fixture_clients = data["fixture_clients"]
        self.fixture_routes = data["fixture_routes"]
        self.fixture_environment = data["fixture_environment"]
        self.http_calls = data["http_calls"]
        self.http_options = data["http_options"]
        self.endpoint_aliases = data["endpoint_aliases"]
        checks = data["uncertainty_checks"]
        self.endpoint_environment = checks["endpoint_environment"]
        self.transport_methods = checks["transport_methods"]
        self.explicit_endpoint_clients = checks["explicit_endpoint_clients"]
        self.protected_fixture_symbols = checks["protected_fixture_symbols"]

    def is_endpoint_alias(self, target, symbol):
        return symbol in self.endpoint_aliases.get(target, ())

    def fixture_route(self, method):
        return self.fixture_routes.get(method)

    def transport_may_affect(self, target, origin):
        return origin in self.transport_methods.get(target, ())

    def endpoint_variables(self, origin):
        return self.endpoint_environment.get(origin, ())

    def has_explicit_endpoint(self, client, keywords):
        return any(name in self.explicit_endpoint_clients.get(client, ()) for name in keywords)


def require_mapping(value, name, fields=None):
    if not isinstance(value, dict) or any(not isinstance(key, str) or not key.strip() for key in value):
        raise ValueError(f"{name} must be an object with nonempty string keys")
    if fields is not None and set(value) != set(fields):
        raise ValueError(f"{name} must contain exactly: {', '.join(fields)}")


def require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def string_set(value, name):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list of strings")
    for item in value:
        require_text(item, name)
    if len(set(value)) != len(value):
        raise ValueError(f"{name} contains duplicate entries")
    return frozenset(value)


def set_mapping(value, name):
    require_mapping(value, name)
    return {key: string_set(items, f"{name}.{key}") for key, items in value.items()}


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate rule key: {key}")
        result[key] = value
    return result


def load_library_rules(path=DATA_DIR / "library_rules.json"):
    """Reject missing or malformed rules instead of silently dropping checks."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot load library rules from {path}: {error}") from error
    require_mapping(data, "library rules", (
        "service_clients", "fixture_clients", "fixture_routes", "fixture_environment",
        "http_calls", "http_options", "endpoint_aliases", "uncertainty_checks",
    ))
    require_mapping(data["service_clients"], "service_clients")
    for client, rule in data["service_clients"].items():
        require_mapping(rule, client, ("endpoint_arguments", "allowed_options"))
        for field in ("endpoint_arguments", "allowed_options"):
            rule[field] = string_set(rule[field], f"{client}.{field}")
        if not rule["endpoint_arguments"] or not rule["endpoint_arguments"] <= rule["allowed_options"]:
            raise ValueError(f"{client}: endpoint arguments must be included in allowed options")
    require_mapping(data["fixture_clients"], "fixture_clients")
    for client, argument in data["fixture_clients"].items():
        require_text(argument, f"fixture_clients.{client}")
        rule = data["service_clients"].get(client)
        if rule is None or argument not in rule["endpoint_arguments"]:
            raise ValueError(f"{client}: fixture endpoint must match a service client rule")
    require_mapping(data["fixture_routes"], "fixture_routes")
    for method, route in data["fixture_routes"].items():
        require_text(route, f"fixture_routes.{method}")
        if not route.startswith("/"):
            raise ValueError(f"{method}: route must start with /")
    require_mapping(data["http_calls"], "http_calls")
    for function, position in data["http_calls"].items():
        if type(position) is not int or position < 0:
            raise ValueError(f"{function}: URL argument position must be a nonnegative integer")
    for field in ("fixture_environment", "http_options"):
        data[field] = string_set(data[field], field)
    data["endpoint_aliases"] = set_mapping(data["endpoint_aliases"], "endpoint_aliases")
    checks = data["uncertainty_checks"]
    require_mapping(checks, "uncertainty_checks", (
        "endpoint_environment", "transport_methods", "explicit_endpoint_clients", "protected_fixture_symbols",
    ))
    for field in ("endpoint_environment", "transport_methods", "explicit_endpoint_clients"):
        checks[field] = set_mapping(checks[field], f"uncertainty_checks.{field}")
    checks["protected_fixture_symbols"] = string_set(checks["protected_fixture_symbols"], "protected_fixture_symbols")
    return LibraryRules(data)


LIBRARY_RULES = load_library_rules()
