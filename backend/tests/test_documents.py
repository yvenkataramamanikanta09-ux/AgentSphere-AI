import pytest
from fastapi.testclient import TestClient

from agentsphere.config import Settings
from agentsphere.main import create_app

URL = "/api/v1/documents/validate"


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("proposal.pdf", "application/pdf"),
        (
            "proposal.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("notes.txt", "text/plain"),
        ("README.md", "text/markdown"),
        ("notes.markdown", "text/markdown"),
        ("Quarterly Report.PDF", "application/pdf"),
        ("résumé.txt", "text/plain"),
    ],
)
def test_supported_documents(client, filename, content_type):
    payload = {"filename": filename, "content_type": content_type, "size_bytes": 1024}
    response = client.post(URL, json=payload)
    assert response.status_code == 200
    assert response.json() == {
        "valid": True,
        "document": payload,
        "max_upload_bytes": 10_000_000,
    }


@pytest.mark.parametrize("size", [1, 9_999_999, 10_000_000])
def test_allowed_size_boundaries(client, metadata, size):
    metadata["size_bytes"] = size
    response = client.post(URL, json=metadata)
    assert response.status_code == 200
    assert response.json()["document"]["size_bytes"] == size


@pytest.mark.parametrize("size", [0, -1, 1.5, 1.0, "1024", True, None])
def test_invalid_sizes(client, metadata, size):
    metadata["size_bytes"] = size
    response = client.post(URL, json=metadata)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "size_bytes"]


def test_size_above_limit(client, metadata):
    metadata["size_bytes"] = 10_000_001
    response = client.post(URL, json=metadata)
    assert response.status_code == 413
    assert "10000000-byte limit" in response.json()["detail"]


@pytest.mark.parametrize(
    "filename",
    [
        "",
        " ",
        " report.pdf",
        "report.pdf ",
        ".pdf",
        "..",
        "report.pdf.",
        "../report.pdf",
        "folder/report.pdf",
        "folder\\report.pdf",
        "C:\\report.pdf",
        "report\x00.pdf",
        "report\n.pdf",
        "report?.pdf",
        "CON.pdf",
        "lpt1.txt",
        "report",
        "report.exe",
        "report.pdf.exe",
        "report.doc",
        "a" * 252 + ".pdf",
        123,
        None,
    ],
)
def test_invalid_filenames(client, metadata, filename):
    metadata["filename"] = filename
    response = client.post(URL, json=metadata)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "filename"]


@pytest.mark.parametrize(
    "filename",
    ["CON .txt", "COM\u00b9.txt", "LPT\u00b2.txt", "lpt\u00b3.txt", "CONIN$.txt", "CONOUT$.txt"],
)
def test_windows_device_aliases_are_rejected_with_matching_content_type(client, filename):
    response = client.post(
        URL, json={"filename": filename, "content_type": "text/plain", "size_bytes": 1}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "filename"]


@pytest.mark.parametrize("filename", ["CONSOLE.txt", "COM0.txt", "COM10.txt", "research .txt"])
def test_non_device_names_remain_valid(client, filename):
    response = client.post(
        URL, json={"filename": filename, "content_type": "text/plain", "size_bytes": 1}
    )
    assert response.status_code == 200


@pytest.mark.parametrize("content_type", ["text/plain", "image/png", "", None, 42])
def test_invalid_or_mismatched_content_type(client, metadata, content_type):
    metadata["content_type"] = content_type
    assert client.post(URL, json=metadata).status_code == 422


def test_content_type_normalization(client, metadata):
    metadata["content_type"] = " Application/PDF "
    response = client.post(URL, json=metadata)
    assert response.status_code == 200
    assert response.json()["document"]["content_type"] == "application/pdf"


@pytest.mark.parametrize("field", ["filename", "content_type", "size_bytes"])
def test_missing_required_fields(client, metadata, field):
    del metadata[field]
    response = client.post(URL, json=metadata)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]


def test_unknown_fields_are_rejected(client, metadata):
    metadata["unexpected"] = "value"
    assert client.post(URL, json=metadata).status_code == 422


def test_malformed_json(client):
    response = client.post(URL, content="{", headers={"Content-Type": "application/json"})
    assert response.status_code == 422


def test_missing_body(client):
    assert client.post(URL).status_code == 422


def test_custom_limit_is_enforced(metadata):
    settings = Settings(_env_file=None, max_upload_bytes=100)
    with TestClient(create_app(settings)) as client:
        metadata["size_bytes"] = 100
        assert client.post(URL, json=metadata).status_code == 200
        metadata["size_bytes"] = 101
        assert client.post(URL, json=metadata).status_code == 413


def test_openapi_documents_contract(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"][URL]["post"]
    assert {"200", "413", "422"} <= operation["responses"].keys()
    request_schema = schema["components"]["schemas"]["DocumentMetadata"]
    assert set(request_schema["required"]) == {"filename", "content_type", "size_bytes"}
