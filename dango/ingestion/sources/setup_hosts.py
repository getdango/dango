"""dango/ingestion/sources/setup_hosts.py

Host-bearing source params: the value that decides where a source's credentials are sent.
"""

import ipaddress
import re
from typing import Any
from urllib.parse import SplitResult, urlsplit

# source type -> the param that decides which host receives the source's credentials
HOST_BEARING_PARAMS: dict[str, str] = {
    "rest_api": "base_url",
    "zendesk": "subdomain",
    "matomo": "url",
    "freshdesk": "domain",
    "jira": "subdomain",
    "workable": "subdomain",
    "inbox": "host",
    "strapi": "domain",
    "filesystem": "bucket_url",
}

# Types whose param is one DNS label placed in front of a fixed vendor domain.
_LABEL_SUFFIX: dict[str, str] = {
    "zendesk": "zendesk.com",
    "freshdesk": "freshdesk.com",
    "jira": "atlassian.net",
    "workable": "workable.com",
}
_HOSTNAME_TYPES = frozenset({"inbox", "strapi"})
_URL_TYPES = frozenset({"rest_api", "matomo"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# fsspec/dlt protocols accepted for filesystem.bucket_url (no scheme = local path).
_FS_SCHEMES = frozenset(
    {"file", "s3", "gs", "gcs", "gdrive", "az", "abfs", "abfss", "adl", "azure", "hf", "sftp"}
)
_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
# Human-entered hostnames may also use '_' (e.g. Docker service names such as my_api).
_LENIENT_LABEL_RE = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?")


def _is_hostname(value: str, *, lenient: bool = False) -> bool:
    """True for a dotted DNS hostname (labels only; no scheme, port, path or userinfo).

    lenient: also allow '_' in labels and one trailing dot (a fully qualified name).
    """
    label = _LENIENT_LABEL_RE if lenient else _LABEL_RE
    if lenient and value.endswith("."):
        value = value[:-1]
    return 0 < len(value) <= 253 and all(label.fullmatch(p) for p in value.split("."))


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _port_ok(parts: SplitResult) -> bool:
    """False when the URL's port is malformed or out of range."""
    try:
        _ = parts.port
    except ValueError:
        return False
    return True


def _url_error(param: str, value: str, require_https: bool, lenient: bool) -> str | None:
    shape = (
        f"'{param}' must be a URL like https://api.example.com/v1 "
        "(no user:password@, no '#' fragment, no '?' query)"
    )
    if lenient and ("?" in value or "#" in value):
        # The endpoint path is appended after the base URL, so a query here breaks every request.
        return (
            f"'{param}' must not contain '?' or '#'. "
            "Put query parameters in the endpoint's params, not the base URL"
        )
    if "#" in value or "?" in value or any(ch.isspace() for ch in value):
        return shape
    parts = urlsplit(value)
    if not _port_ok(parts):
        return shape
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host or "@" in parts.netloc:
        return shape
    if not (_is_hostname(host, lenient=lenient) or _is_ip(host)):
        return shape
    if require_https and parts.scheme == "http" and host not in LOOPBACK_HOSTS:
        return f"'{param}' must use https:// (http:// is only allowed for localhost)"
    return None


def _bucket_url_error(param: str, value: str) -> str | None:
    if any(ch.isspace() for ch in value) or "#" in value or "?" in value:
        return f"'{param}' must not contain whitespace, '#' or '?'"
    parts = urlsplit(value)
    if len(parts.scheme) <= 1:  # local path (or a Windows drive letter)
        return None
    if parts.scheme not in _FS_SCHEMES or "@" in parts.netloc:
        return (
            f"'{param}' must be a local path or a storage URL such as s3://bucket/path "
            f"(supported: {', '.join(sorted(_FS_SCHEMES))}; no user@ part)"
        )
    if parts.scheme == "sftp" and not (_is_hostname(parts.hostname or "") and _port_ok(parts)):
        return f"'{param}' must be sftp://<hostname>[:port]/<path> (no user@ part)"
    return None


def validate_host_param(
    source_type: str, value: Any, *, require_https: bool = False, human_entry: bool = False
) -> str | None:
    """Return an error string, or None if *value* is acceptable for *source_type*'s host param.

    require_https: reject plain http:// for non-loopback hosts (the agent path); otherwise
    such a value is accepted and :func:`insecure_host_warning` reports it.
    human_entry: a person typed a URL in the CLI wizard: also accept '_' in the hostname and
    one trailing dot. Never used for agent input.
    """
    param = HOST_BEARING_PARAMS.get(source_type)
    if param is None:
        return None
    if not isinstance(value, str):
        return f"'{param}' must be a string"
    if source_type in _LABEL_SUFFIX:
        if _LABEL_RE.fullmatch(value):
            return None
        return (
            f"'{param}' must be just the {source_type} subdomain: enter e.g. acme, "
            f"not acme.{_LABEL_SUFFIX[source_type]} or a URL"
        )
    if source_type in _HOSTNAME_TYPES:
        if _is_hostname(value):
            return None
        return (
            f"'{param}' must be a hostname such as imap.example.com "
            "(no https://, port, path, user@ part, '#' or '?')"
        )
    if source_type in _URL_TYPES:
        return _url_error(param, value, require_https, human_entry)
    return _bucket_url_error(param, value)


def insecure_host_warning(source_type: str, value: Any) -> str | None:
    """Warning text when a URL-type host param uses plain http:// for a non-loopback host."""
    if source_type not in _URL_TYPES or not isinstance(value, str):
        return None
    parts = urlsplit(value)
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        param = HOST_BEARING_PARAMS[source_type]
        return f"'{param}' uses http://: credentials for this source will be sent unencrypted"
    return None


def destination_host(source_type: str, params: dict[str, Any]) -> str | None:
    """The host a source's credentials are sent to, derived the way the connector builds it."""
    value = params.get(HOST_BEARING_PARAMS.get(source_type, ""))
    if not isinstance(value, str) or not value:
        return None
    try:
        if source_type in _LABEL_SUFFIX:
            return urlsplit(f"https://{value}.{_LABEL_SUFFIX[source_type]}/").hostname
        if source_type == "strapi":
            return urlsplit(f"https://{value}/").hostname
        if source_type == "inbox":
            return value
        if source_type in _URL_TYPES:
            return urlsplit(value).hostname
    except ValueError:
        return None
    return None
