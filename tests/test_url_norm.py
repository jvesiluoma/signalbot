import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import url_norm


def test_normalize_basic():
    assert url_norm.normalize_url("HTTPS://www.Example.com/Foo/?b=2&a=1#frag") == "https://example.com/Foo?a=1&b=2"


def test_normalize_strips_tracking():
    assert url_norm.normalize_url("https://ex.com/a?utm_source=x&fbclid=y&keep=1") == "https://ex.com/a?keep=1"


def test_normalize_default_ports():
    assert url_norm.normalize_url("http://ex.com:80/") == "http://ex.com"
    assert url_norm.normalize_url("https://ex.com:443/x/") == "https://ex.com/x"
    assert url_norm.normalize_url("https://ex.com:8443/x") == "https://ex.com:8443/x"


def test_normalize_rejects_non_http():
    assert url_norm.normalize_url("ftp://ex.com/x") is None
    assert url_norm.normalize_url("not a url") is None
    assert url_norm.normalize_url("") is None
    assert url_norm.normalize_url(None) is None


def test_extract_domain():
    assert url_norm.extract_domain("https://www.bbc.co.uk/news") == "bbc.co.uk"
    assert url_norm.extract_domain("http://sub.example.com:8080/x") == "sub.example.com"
    assert url_norm.extract_domain("garbage") is None
