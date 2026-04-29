"""Tests for web fetch and search tools."""
import asyncio
import ipaddress
import pytest

from tools.fetch import _check_ssrf, _strip_html, SSRFError


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------

def test_ssrf_blocks_localhost():
    with pytest.raises(SSRFError):
        _check_ssrf("127.0.0.1")


def test_ssrf_blocks_private_10():
    with pytest.raises(SSRFError):
        _check_ssrf("10.0.0.1")


def test_ssrf_blocks_tailscale_cgnat():
    with pytest.raises(SSRFError):
        _check_ssrf("100.100.214.61")


def test_ssrf_allows_public(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "93.184.216.34")
    _check_ssrf("example.com")   # should not raise


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

def test_strip_html_removes_tags():
    html = "<h1>Title</h1><p>Body text</p>"
    result = _strip_html(html)
    assert "<" not in result
    assert "Title" in result
    assert "Body text" in result


def test_strip_html_removes_script():
    html = "<p>Visible</p><script>alert('xss')</script>"
    result = _strip_html(html)
    assert "alert" not in result
    assert "Visible" in result


def test_strip_html_decodes_entities():
    html = "&lt;tag&gt; &amp; value"
    result = _strip_html(html)
    assert "&lt;" not in result
    assert "&gt;" not in result
    assert "&amp;" not in result
    assert "value" in result
