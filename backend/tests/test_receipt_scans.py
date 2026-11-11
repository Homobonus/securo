"""Receipt photos: read by a vision model, corrected by a person, then stored.

The model is never trusted with the last word. What it reads is a draft;
only an accepted draft becomes a receipt, and only then do its prices
enter a product's history.
"""
import io
import json
import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import select

from app.models.receipt import Receipt, ReceiptScan, ShoppingProduct
from app.providers import receipt_scan
from app.providers.receipt_scan import ReceiptScanError, ScanSettings, extract, prepare_image
from app.services import receipt_scan_service as scans
from app.models.account import Account
from tests.test_receipts import debit

ENABLED = ScanSettings(api_url="https://api.openai.com/v1", api_key="sk-test", model="gpt-test", timeout=5)
DISABLED = ScanSettings(api_url="https://api.openai.com/v1", api_key="", model="gpt-test", timeout=5)


def jpeg(width=40, height=120, orientation=None) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    out = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(out, format="JPEG", exif=exif)
    else:
        image.save(out, format="JPEG")
    return out.getvalue()


ZABKA = {
    "is_receipt": True,
    "store_name": "Żabka",
    "store_address": "ul. Prosta 1, Warszawa",
    "tax_id": "PL 527-26-36-353",
    "purchased_at": "2026-11-05T18:42",
    "currency": "pln",
    "total": 21.47,
    "items": [
        {"name": "Kawa Kimbo 250g", "quantity": 1, "unit": None, "unit_price": 19.99, "total": 19.99, "discount": -3.0},
        {"name": "Banany", "quantity": 0.746, "unit": "KG", "unit_price": 5.99, "total": 4.47, "discount": 0},
        {"name": "", "quantity": 1, "unit": None, "unit_price": 1, "total": 1, "discount": 0},
    ],
}


@pytest_asyncio.fixture
async def account(session, test_user, test_workspace) -> Account:
    acc = Account(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, name="Konto",
        type="checking", balance=Decimal("0"), currency="PLN",
    )
    session.add(acc)
    await session.commit()
    return acc


def completion(payload: dict, finish_reason: str = "stop") -> dict:
    return {"choices": [{"finish_reason": finish_reason, "message": {"content": json.dumps(payload)}}]}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class TestImage:
    def test_a_sideways_phone_photo_is_stored_upright_and_shrunk(self):
        # EXIF 6: the camera was turned 90°, the pixels were not.
        prepared = Image.open(io.BytesIO(prepare_image(jpeg(3000, 900, orientation=6))))
        assert prepared.format == "JPEG"
        assert prepared.size[1] > prepared.size[0]
        assert max(prepared.size) == receipt_scan.MAX_IMAGE_SIDE

    def test_something_that_is_not_an_image_is_refused(self):
        with pytest.raises(ReceiptScanError) as error:
            prepare_image(b"%PDF-1.7 not a photo")
        assert error.value.code == "unsupported_image"


class TestExtract:
    @pytest.mark.asyncio
    async def test_the_photo_goes_out_with_a_schema_and_comes_back_parsed(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=completion(ZABKA))

        result = await extract(b"jpeg-bytes", config=ENABLED, transport=httpx.MockTransport(handler))

        assert result["store_name"] == "Żabka"
        assert seen["url"] == "https://api.openai.com/v1/chat/completions"
        assert seen["auth"] == "Bearer sk-test"
        body = seen["body"]
        assert body["model"] == "gpt-test"
        assert body["response_format"]["json_schema"]["strict"] is True
        image = body["messages"][1]["content"][1]["image_url"]["url"]
        assert image.startswith("data:image/jpeg;base64,")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("response", "code"),
        [
            (httpx.Response(401, json={"error": {"code": "invalid_api_key"}}), "auth"),
            (httpx.Response(429, json={"error": {"code": "insufficient_quota"}}), "quota"),
            (httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}}), "rate_limited"),
            (httpx.Response(500, text="boom"), "provider_error"),
            (httpx.Response(200, json=completion({**ZABKA, "is_receipt": False, "items": []})), "not_a_receipt"),
            (httpx.Response(200, json=completion(ZABKA, finish_reason="length")), "too_long"),
            (httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]}), "bad_response"),
        ],
    )
    async def test_failures_come_back_as_stable_codes(self, response, code):
        with pytest.raises(ReceiptScanError) as error:
            await extract(b"x", config=ENABLED, transport=httpx.MockTransport(lambda _: response))
        assert error.value.code == code

    @pytest.mark.asyncio
    async def test_nothing_is_sent_without_a_key(self):
        def handler(request):  # pragma: no cover - must not be called
            raise AssertionError("sent a request while disabled")

        with pytest.raises(ReceiptScanError) as error:
            await extract(b"x", config=DISABLED, transport=httpx.MockTransport(handler))
        assert error.value.code == "not_configured"

    def test_an_own_server_needs_no_key(self):
        assert ScanSettings(api_url="http://ollama:11434/v1", api_key="", model="m", timeout=5).enabled


class TestDraft:
    def test_the_models_answer_is_cleaned_into_an_editable_draft(self):
        draft = scans.draft_from_model(ZABKA)

        assert draft.date == date(2026, 11, 5)
        assert draft.time == "18:42"
        assert draft.currency == "PLN"
        assert draft.tax_id == "5272636353"
        # The nameless line is dropped, the negative discount is an amount off.
        assert [(i.name, i.quantity, i.unit, i.total, i.discount) for i in draft.items] == [
            ("Kawa Kimbo 250g", Decimal("1.000"), None, Decimal("19.99"), Decimal("3.00")),
            ("Banany", Decimal("0.746"), "kg", Decimal("4.47"), Decimal("0.00")),
        ]
        assert scans.items_sum(draft.items) == Decimal("21.46")

    def test_a_date_without_a_time_is_kept(self):
        draft = scans.draft_from_model({"purchased_at": "2026-11-05", "items": []})
        assert (draft.date, draft.time) == (date(2026, 11, 5), None)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class MemoryStorage:
    def __init__(self):
        self.files: dict[str, bytes] = {}

    async def upload(self, key, data, content_type):
        self.files[key] = data

    async def download(self, key):
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    async def delete(self, key):
        self.files.pop(key, None)


@pytest.fixture
def storage(monkeypatch) -> MemoryStorage:
    store = MemoryStorage()
    monkeypatch.setattr(scans, "get_storage_provider", lambda: store)
    monkeypatch.setattr("app.api.receipt_scans.get_storage_provider", lambda: store)
    return store


@pytest.fixture
def model(monkeypatch):
    """A configured model whose answer each test sets."""
    state: dict = {"answer": ZABKA, "calls": 0}

    async def fake_extract(image, *, config=None, transport=None):
        state["calls"] += 1
        answer = state["answer"]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(scans, "scan_settings", lambda: ENABLED)
    monkeypatch.setattr("app.api.receipt_scans.scan_settings", lambda: ENABLED)
    monkeypatch.setattr(scans, "extract", fake_extract)
    return state


async def upload(client, auth_headers, data: bytes | None = None):
    files = {"file": ("IMG_0001.jpg", data or jpeg(), "image/jpeg")}
    return await client.post("/api/receipt-scans", files=files, headers=auth_headers)


def accepted(draft: dict, **changes) -> dict:
    return {**draft, **changes}


@pytest.mark.asyncio
async def test_without_a_model_the_feature_is_off(client, auth_headers, monkeypatch, storage):
    monkeypatch.setattr("app.api.receipt_scans.scan_settings", lambda: DISABLED)

    assert (await client.get("/api/receipt-scans/status", headers=auth_headers)).json() == {
        "enabled": False,
        "model": None,
    }
    response = await upload(client, auth_headers)
    assert response.status_code == 409
    assert storage.files == {}


@pytest.mark.asyncio
async def test_a_photo_becomes_a_receipt_only_once_accepted(
    client, auth_headers, session, storage, model, account
):
    paid = await debit(session, account, date(2026, 11, 5), "-21.47", description="ZABKA Z5123 K.1 WARSZAWA")

    response = await upload(client, auth_headers)
    assert response.status_code == 201, response.text
    scan = response.json()
    assert scan["status"] == "ready"
    assert scan["draft"]["store_name"] == "Żabka"
    assert Decimal(scan["items_sum"]) == Decimal("21.46")
    assert len(storage.files) == 1
    # A draft is not a receipt yet.
    assert (await session.execute(select(Receipt))).scalars().all() == []
    listed = (await client.get("/api/receipt-scans", headers=auth_headers)).json()
    assert [s["id"] for s in listed] == [scan["id"]]
    image = await client.get(f"/api/receipt-scans/{scan['id']}/image", headers=auth_headers)
    assert image.headers["content-type"] == "image/jpeg"

    # The person fixes the misread banana price, then accepts.
    draft = scan["draft"]
    draft["items"][1]["total"] = "4.48"
    draft["total"] = "21.47"
    response = await client.post(f"/api/receipt-scans/{scan['id']}/accept", json=draft, headers=auth_headers)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["source"] == "scan"
    assert receipt["external_id"] == scan["id"]
    assert Decimal(receipt["total"]) == Decimal("21.47")
    assert Decimal(receipt["discount_total"]) == Decimal("3.00")
    assert [i["name"] for i in receipt["items"]] == ["Kawa Kimbo 250g", "Banany"]
    # Paired with the card payment: same amount, and "ZABKA" names "Żabka".
    assert receipt["transaction_id"] == str(paid.id)
    assert receipt["match_method"] == "auto"
    products = (await session.execute(select(ShoppingProduct.name))).scalars().all()
    assert sorted(products) == ["Banany", "Kawa Kimbo 250g"]
    assert (await client.get("/api/receipt-scans", headers=auth_headers)).json() == []

    # Deleting the receipt takes the photo with it.
    assert (await client.delete(f"/api/receipts/{receipt['id']}", headers=auth_headers)).status_code == 204
    assert storage.files == {}
    assert (await session.execute(select(ReceiptScan))).scalars().all() == []


@pytest.mark.asyncio
async def test_accepting_again_rewrites_the_same_receipt(client, auth_headers, session, storage, model):
    scan = (await upload(client, auth_headers)).json()
    draft = accepted(scan["draft"], total="21.46")
    first = (await client.post(f"/api/receipt-scans/{scan['id']}/accept", json=draft, headers=auth_headers)).json()

    draft["store_name"] = "Żabka Nano"
    second = (await client.post(f"/api/receipt-scans/{scan['id']}/accept", json=draft, headers=auth_headers)).json()

    assert second["id"] == first["id"]
    assert second["store_name"] == "Żabka Nano"
    assert len((await session.execute(select(Receipt))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_a_draft_without_lines_or_total_cannot_be_accepted(client, auth_headers, storage, model):
    scan = (await upload(client, auth_headers)).json()
    no_lines = accepted(scan["draft"], items=[])
    response = await client.post(f"/api/receipt-scans/{scan['id']}/accept", json=no_lines, headers=auth_headers)
    assert response.status_code == 422
    no_total = accepted(scan["draft"], total=None)
    response = await client.post(f"/api/receipt-scans/{scan['id']}/accept", json=no_total, headers=auth_headers)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_a_failed_read_keeps_the_photo_for_a_retry(client, auth_headers, storage, model):
    model["answer"] = ReceiptScanError("timeout")
    scan = (await upload(client, auth_headers)).json()
    assert (scan["status"], scan["error"], scan["draft"]) == ("failed", "timeout", None)
    assert len(storage.files) == 1

    model["answer"] = ZABKA
    retried = (await client.post(f"/api/receipt-scans/{scan['id']}/retry", headers=auth_headers)).json()
    assert retried["status"] == "ready"
    assert retried["error"] is None
    assert model["calls"] == 2


@pytest.mark.asyncio
async def test_discarding_a_draft_removes_the_photo(client, auth_headers, session, storage, model):
    scan = (await upload(client, auth_headers)).json()
    assert (await client.delete(f"/api/receipt-scans/{scan['id']}", headers=auth_headers)).status_code == 204
    assert storage.files == {}
    assert (await client.get(f"/api/receipt-scans/{scan['id']}", headers=auth_headers)).status_code == 404


@pytest.mark.asyncio
async def test_a_file_that_is_not_a_photo_is_refused(client, auth_headers, storage, model):
    response = await upload(client, auth_headers, data=b"%PDF-1.7")
    assert response.status_code == 415
    assert response.json()["detail"] == "unsupported_image"
    assert model["calls"] == 0


@pytest.mark.asyncio
async def test_another_workspaces_scan_is_not_found(client, auth_headers, session, storage, model, test_user):
    from app.models.workspace import Workspace

    other = Workspace(id=uuid.uuid4(), name="Other", created_by_user_id=test_user.id)
    session.add(other)
    await session.flush()
    stranger = ReceiptScan(workspace_id=other.id, storage_key="x", content_type="image/jpeg", status="ready")
    session.add(stranger)
    await session.commit()
    assert (await client.get(f"/api/receipt-scans/{stranger.id}", headers=auth_headers)).status_code == 404
    assert (await client.get(f"/api/receipt-scans/{stranger.id}/image", headers=auth_headers)).status_code == 404
