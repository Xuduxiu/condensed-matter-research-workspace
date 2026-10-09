import httpx
import pytest

from paper_intake.models import Paper
from paper_intake.zotero import ZoteroLocalClient
from paper_intake.zotero.errors import ZoteroLocalError
from paper_intake.zotero.local_client import DISABLED_WRITE_MESSAGE


def _client(handler):
    return ZoteroLocalClient(
        base_url="http://localhost:23119/api",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_health_check_gracefully_fails_when_zotero_is_not_running():
    def handler(_request):
        raise httpx.ConnectError("connection refused")

    status = _client(handler).health_check()

    assert status.connected is False
    assert "未连接到 Zotero" in status.error_message


def test_health_check_can_mock_success():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=[])

    status = _client(handler).health_check()

    assert status.connected is True
    assert status.error_message == ""


def test_health_check_http_400_403_returns_clear_chinese_message():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(400, text="Endpoint does not support method")

    status = _client(handler).health_check()

    assert status.connected is False
    assert "HTTP 400" in status.error_message
    assert "RIS" in status.error_message


def test_write_methods_are_disabled_and_do_not_post_to_local_api():
    requests = []

    def handler(request):
        requests.append(request)
        raise AssertionError("Local API should not receive write requests")

    client = _client(handler)
    with pytest.raises(ZoteroLocalError, match="write operations are disabled"):
        client.create_collection("Lab Paper Intake")
    with pytest.raises(ZoteroLocalError, match="write operations are disabled"):
        client.create_item(Paper(title="Paper"))
    with pytest.raises(ZoteroLocalError, match="write operations are disabled"):
        client.create_note("ITEM", "<p>note</p>")
    with pytest.raises(ZoteroLocalError, match="write operations are disabled"):
        client.add_item_to_collection("ITEM", "COLL")
    with pytest.raises(ZoteroLocalError, match="write operations are disabled"):
        client.create_link_attachment("ITEM", "PDF", "https://example.org/a.pdf")
    with pytest.raises(ZoteroLocalError, match="write operations are disabled"):
        client.import_local_pdf_attachment("ITEM", "pdfs/a.pdf")
    with pytest.raises(ZoteroLocalError, match="write operations are disabled"):
        client.add_tags({"key": "ITEM"}, ["tag"])

    assert requests == []
    assert "RIS package import" in DISABLED_WRITE_MESSAGE


def test_find_existing_item_uses_get_only_and_prefers_exact_doi():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(
            200,
            json=[
                {"data": {"key": "A", "DOI": "10.1000/test", "title": "Different"}},
                {"data": {"key": "B", "title": "Origin of Hall Effect"}},
            ],
        )

    existing = _client(handler).find_existing_item("10.1000/test", "Origin of Hall Effect")

    assert existing["data"]["key"] == "A"