"""
URL normalization helpers shared by the ingest path and the cross-platform
analytics endpoints.

`normalize_url()` produces a stable key for "the same link" so that the same
article posted on Signal, Telegram and WhatsApp — possibly with different
tracking parameters or trailing slashes — collapses to one row in
`url_observations` and one node in the URL-spread graph.

`extract_domain()` returns the registrable-ish host (we keep it simple: the
hostname minus a leading "www.") for domain-level aggregation.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# Query parameters that are pure tracking / campaign noise — dropped during
# normalization. Lower-cased comparison.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_social", "utm_brand",
    "gclid", "gclsrc", "dclid", "fbclid", "msclkid", "mc_eid", "mc_cid",
    "igshid", "ig_rid", "yclid", "twclid", "ttclid", "rb_clickid",
    "_hsenc", "_hsmi", "vero_id", "vero_conv", "wickedid", "oly_anon_id",
    "oly_enc_id", "spm", "scm", "ref_src", "ref_url", "s_cid", "cmpid",
    "ncid", "mkt_tok", "trk", "trkCampaign", "guccounter",
})


def normalize_url(url):
    """Return a normalized form of `url`, or None if it isn't a usable http(s) URL.

    Transformations:
      * scheme/host lower-cased
      * default ports stripped (:80 for http, :443 for https)
      * a leading "www." removed from the host
      * userinfo (user:pass@) removed
      * fragment removed
      * tracking query params (utm_*, fbclid, gclid, …) removed
      * remaining query params sorted for stability
      * a lone trailing "/" on the path removed (but "/" itself kept)
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None

    host = (parts.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    port = parts.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"

    path = parts.path or ""
    # Collapse a lone "/" and any trailing slash → no trailing slash (so
    # https://a.com, https://a.com/ and https://a.com/x/ all dedup cleanly).
    if path == "/" or (len(path) > 1 and path.endswith("/")):
        path = path.rstrip("/")

    kept = [
        (k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    kept.sort()
    query = urlencode(kept, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def extract_domain(url):
    """Return the host (minus a leading 'www.') for `url`, or None.

    Accepts either a raw or already-normalized URL.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        host = (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host
