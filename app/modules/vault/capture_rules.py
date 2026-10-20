"""Which pages the extension should read more than a picture from.

A capture normally saves a picture and a title. For some sites the picture is
barely half of what is worth keeping: an illustration is its id, its author and
the tags it was published under, and a page that keeps only the bytes loses the
thing you came back for. A *rule* says "on this host, on this path, read these
few fields" — and it is deliberately **data, not code**.

That distinction is the whole security shape of this feature. The extension
downloads these rules from the instance it already trusts and runs the selectors
in the page; if a rule could carry a function, then a compromised server — or a
hosted copy of this one — would be a way to run code in every browser the owner
has, on every page they visit. So a rule is a host pattern, a path pattern and a
map of CSS selectors to attributes, and that is the whole language.

Two rules about what a rule may name, both consequences of decisions made
elsewhere in this module:

* **A rule may not name the site in `node_type`.** That column is structural: it
  stays readable while the vault is locked, because the grid lays out its tiles
  from it before anything is decrypted. A value like `pixiv` there publishes the
  owner's browsing to anyone holding the database, whatever the encryption says.
  So a rule may suggest a *neutral* type, and the site goes into the sealed
  `site` field instead.
* **A rule may not become unbounded.** Every field it reads is cut to a length
  and a count, here as well as in the extension, so a selector that matches ten
  thousand nodes costs a card a few kilobytes and not the collection.

Rules live in two places. The shipped ones are in `capture_rules.json` beside
this module; the owner's own are JSON lists in `*_rules.py` files in
`local_types/`, which is git-ignored — so a rule for a site nobody else cares
about costs no committed change and never lands in a push.
"""

import importlib
import json
import logging
import pkgutil
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.modules.vault.node_types import NodeType, looks_like_provenance

logger = logging.getLogger(__name__)

# Where the shipped rules live, and where an owner's own go.
SHIPPED_RULES_PATH = Path(__file__).resolve().parent / "capture_rules.json"
LOCAL_RULES_PACKAGE = "app.modules.vault.local_types"
# A local file only counts as rules if its name says so, next to the `VIEWS`
# dict that names card types: one directory, two kinds of file, no ambiguity
# about which is which.
LOCAL_RULES_SUFFIX = "_rules"

# The language a rule is written in. These are not advisory: a rule that does
# not fit is skipped, because a rule that half-works is worse than no rule — it
# would save a card that looks right and is missing the field you matched it for.
MAX_RULES = 100
MAX_FIELDS_PER_RULE = 30
MAX_SELECTOR_CHARS = 500
MAX_NAME_CHARS = 80
MAX_HOST_CHARS = 253
# A rule reads attributes off elements it found, not text of unbounded length;
# anything past this is a page that wants to be a page, not a field.
MAX_VALUE_CHARS = 500
MAX_VALUES_PER_FIELD = 40
# A capture pattern is the owner's own and runs in the page; long enough for a URL
# shape, short enough that it is obviously not a program.
MAX_PATTERN_CHARS = 200

# The only attribute names a rule may ask for. Reading an inline `style` would
# let a rule pull a data: URL of arbitrary size through a field meant to hold an
# id. `src` is here because a poster address is a legitimate thing to read, and
# it is bounded by the same value ceiling as everything else.
ALLOWED_ATTR_NAMES = frozenset(
    {
        "text",
        "alt",
        "content",
        "datetime",
        "href",
        "rel",
        "src",
        "title",
        "value",
        "data-id",
        "data-illust-id",
        "data-page-count",
        "data-page-id",
        "data-rating",
        "data-user-id",
        "data-user-name",
    }
)


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit].strip()


def validate_rule(raw: Any) -> dict[str, Any] | None:
    """One rule in the shape the extension expects, or None if it does not fit.

    Skipping rather than raising is deliberate throughout: the endpoint that
    serves these must never be the reason a capture fails, and a hand-written
    rule is exactly the thing most likely to contain a typo.
    """
    if not isinstance(raw, dict):
        return None
    rule_id = _text(raw.get("id"), MAX_NAME_CHARS)
    match = raw.get("match")
    if not rule_id or not isinstance(match, dict):
        return None
    host = _text(match.get("host"), MAX_HOST_CHARS).lower()
    if not host:
        return None
    path_pattern = match.get("path")
    if not isinstance(path_pattern, str) or not path_pattern:
        return None
    try:
        compiled = re.compile(path_pattern)
    except re.error:
        logger.warning("the capture rule %r has an unparseable path pattern", rule_id)
        return None

    # The one thing a rule may not do. Not a warning and not "cleaned up": a
    # card type is read straight out of the row by anyone holding the database,
    # sealed vault or not, so a site named there publishes the browsing that the
    # sealed `site` field exists to keep private.
    node_type = _text(raw.get("node_type") or NodeType.IMAGE, MAX_NAME_CHARS)
    if looks_like_provenance(node_type):
        logger.warning(
            "the capture rule %r asks for a card type named after its source; the type column is "
            "readable while the vault is locked, so the rule was skipped",
            rule_id,
        )
        return None

    fields_raw = raw.get("fields")
    if not isinstance(fields_raw, dict):
        return None
    fields: dict[str, dict[str, Any]] = {}
    for name, spec in list(fields_raw.items())[:MAX_FIELDS_PER_RULE]:
        key = _text(name, MAX_NAME_CHARS)
        if not key or not isinstance(spec, dict):
            continue
        selector = _text(spec.get("selector"), MAX_SELECTOR_CHARS)
        attr = _text(spec.get("attr") or "text", MAX_NAME_CHARS).lower()
        if not selector or attr not in ALLOWED_ATTR_NAMES:
            logger.warning("the capture rule %r field %r is not expressible and was skipped", rule_id, key)
            continue
        try:
            maximum = int(spec.get("max") or MAX_VALUES_PER_FIELD)
        except (TypeError, ValueError):
            maximum = MAX_VALUES_PER_FIELD
        field: dict[str, Any] = {
            "selector": selector,
            "attr": attr,
            "many": bool(spec.get("many")),
            "max": max(1, min(maximum, MAX_VALUES_PER_FIELD)),
        }
        # A pattern is here for the single most common shape on the web: an id
        # that lives inside a URL. `og:image` on an illustration page ends in
        # `.../98765432_p0_master1200.jpg` and the id is the thing worth keeping —
        # but a rule that can only read whole attributes would store the whole URL
        # under the name `illust_id`, which is the half-a-rule problem this module
        # refuses elsewhere.
        pattern = spec.get("regex")
        if pattern is not None:
            text = str(pattern)
            if len(text) > MAX_PATTERN_CHARS:
                logger.warning("the capture rule %r field %r has an oversized pattern", rule_id, key)
                continue
            try:
                re.compile(text)
            except re.error:
                # The field is dropped rather than shipped without its pattern.
                # A rule that says "the id inside this URL" and cannot say which
                # part would otherwise store the whole URL under the name `id`,
                # which is the half-a-rule failure this module refuses everywhere
                # else — and it would be invisible, because the field is there.
                logger.warning(
                    "the capture rule %r field %r has an unparseable pattern and was skipped",
                    rule_id,
                    key,
                )
                continue
            field["regex"] = text
            field["group"] = max(0, min(int(spec.get("group") or 1), 32))
        fields[key] = field
    if not fields:
        return None

    return {
        "id": rule_id,
        "site": _text(raw.get("site"), MAX_NAME_CHARS) or host,
        "node_type": node_type,
        "match": {"host": host, "path": path_pattern},
        "fields": fields,
        # Kept out of the payload the extension receives: `public_shape` strips
        # it, because a compiled pattern is not JSON and the extension has to
        # match the URL with the source pattern anyway.
        "_path_re": compiled,
    }


def _shipped() -> list[Any]:
    try:
        raw = json.loads(SHIPPED_RULES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        # A broken rules file costs the owner their own rules, not the dashboard
        # and not every capture.
        logger.warning("could not read the Vault capture rules", exc_info=True)
        return []
    return raw if isinstance(raw, list) else []


def local() -> list[Any]:
    """The owner's own rules, from `*_rules.py` files in the ignored directory."""
    found: list[Any] = []
    try:
        package = importlib.import_module(LOCAL_RULES_PACKAGE)
    except ImportError:
        return found
    for info in pkgutil.iter_modules(package.__path__):
        if not info.name.endswith(LOCAL_RULES_SUFFIX):
            continue
        module_name = f"{LOCAL_RULES_PACKAGE}.{info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception:
            logger.warning("could not load the local capture rules in %s", module_name, exc_info=True)
            continue
        rules = getattr(module, "RULES", None)
        if isinstance(rules, list):
            found.extend(rules)
        else:
            logger.warning("the local capture rules in %s declare no RULES list", module_name)
    return found


def rules() -> list[dict[str, Any]]:
    """Every usable rule, shipped first so a local one can shadow it.

    Shadowing is by `id`, and that is the only kind an owner needs: "the site
    changed its markup, use my selectors". It keeps working because the extension
    keys nothing on order — it matches the first rule that claims the page, so a
    replacement has to be able to unseat the original rather than queue behind it.

    Read once. `rule_for_url` runs on every capture and re-validating every rule
    on every one of them would put a filesystem walk and a burst of warnings in
    the middle of saving a picture. The cost is the same one the local card types
    carry: a local rule added while the process runs needs a restart.
    """
    global _LOADED
    if _LOADED is None:
        _LOADED = _load_all()
    return _LOADED


_LOADED: list[dict[str, Any]] | None = None


def forget() -> None:
    """Drop the snapshot, so the next read sees files on disk again.

    For tests, and for anything that genuinely adds a rule at runtime — which
    nothing in the application does today.
    """
    global _LOADED
    _LOADED = None


def _load_all() -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for raw in _shipped() + local():
        rule = validate_rule(raw)
        if rule is None:
            continue
        merged[rule["id"]] = rule
        if len(merged) > MAX_RULES:
            logger.warning("stopped loading Vault capture rules at %s", MAX_RULES)
            break
    return list(merged.values())


def rule_for_url(url: str | None) -> dict[str, Any] | None:
    """The first rule that claims this page, or None.

    The host match is exact or a subdomain of the rule's host, so a rule written
    for `example.com` also covers `www.example.com` but never `notexample.com`.
    The path pattern is the rule author's, applied to the path only — a query
    string is not part of the page's shape and would let one rule claim all of
    a site.
    """
    if not url:
        return None
    parsed = urlparse(str(url))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    path = parsed.path or "/"
    for rule in rules():
        wanted = rule["match"]["host"]
        if host != wanted and not host.endswith("." + wanted):
            continue
        if rule["_path_re"].search(path):
            return rule
    return None


def public_shape(rule: dict[str, Any]) -> dict[str, Any]:
    """A rule as the extension receives it, with no compiled pattern attached."""
    return {key: value for key, value in rule.items() if not key.startswith("_")}


__all__ = [
    "ALLOWED_ATTR_NAMES",
    "MAX_FIELDS_PER_RULE",
    "MAX_PATTERN_CHARS",
    "MAX_RULES",
    "MAX_SELECTOR_CHARS",
    "MAX_VALUES_PER_FIELD",
    "MAX_VALUE_CHARS",
    "forget",
    "local",
    "public_shape",
    "rule_for_url",
    "rules",
    "validate_rule",
]
