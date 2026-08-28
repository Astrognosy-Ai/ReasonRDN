from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from rdn.doclang_adapter import (
    DOCLANG_MEDIA_TYPE,
    DocLangAdapterError,
    PreparedDocLangContribution,
    prepare_doclang_contribution,
)


VALID_DOCLANG = b'''<?xml version="1.0" encoding="UTF-8"?>
<doclang xmlns="https://www.doclang.ai/ns/v0" version="0.7">
  <heading level="1">Resolver memory</heading>
  <text>Resolve this artifact before repeating the task.</text>
</doclang>
'''


def test_existing_doclang_is_preserved_as_exact_bytes(tmp_path: Path) -> None:
    source = tmp_path / "artifact.dclg"
    source.write_bytes(VALID_DOCLANG)

    prepared = prepare_doclang_contribution(source, validation="structural")

    assert isinstance(prepared, PreparedDocLangContribution)
    assert prepared.content == VALID_DOCLANG
    assert prepared.media_type == DOCLANG_MEDIA_TYPE
    assert prepared.adapter["spec_version"] == "0.7"
    expected_digest = hashlib.sha256(VALID_DOCLANG).hexdigest()
    assert prepared.adapter["source"]["digest"]["value"] == expected_digest
    assert prepared.adapter["output"]["digest"]["value"] == expected_digest
    assert prepared.adapter["validation"] == {
        "status": "valid",
        "method": "bounded-structural",
    }


def test_doclang_envelope_binds_adapter_and_exact_content(tmp_path: Path) -> None:
    source = tmp_path / "artifact.dclg"
    source.write_bytes(VALID_DOCLANG)
    prepared = prepare_doclang_contribution(source, validation="structural")

    envelope = prepared.envelope(
        "reason://agents/memory/resolve-first",
        scope="organization",
        created_at="2026-08-22T00:00:00+00:00",
    )

    assert envelope.content_bytes() == VALID_DOCLANG
    assert envelope.artifact["media_type"] == DOCLANG_MEDIA_TYPE
    assert envelope.adapter == prepared.adapter
    assert envelope.contribution_id.startswith("sha256:")


def test_prepared_document_queues_through_the_normal_client_surface(tmp_path: Path) -> None:
    source = tmp_path / "artifact.dclg"
    source.write_bytes(VALID_DOCLANG)
    prepared = prepare_doclang_contribution(source, validation="structural")
    captured = {}

    class Client:
        @staticmethod
        def contribute(content, **kwargs):
            captured.update(content=content, kwargs=kwargs)
            return {"status": "pending", "contribution_id": "sha256:" + "a" * 64}

    result = prepared.contribute(
        Client(),
        "reason://agents/memory/resolve-first",
        scope="shared",
        background=False,
    )

    assert result["status"] == "pending"
    assert captured["content"] == VALID_DOCLANG
    assert captured["kwargs"]["media_type"] == DOCLANG_MEDIA_TYPE
    assert captured["kwargs"]["adapter"] == prepared.adapter
    assert captured["kwargs"]["scope"] == "shared"
    assert captured["kwargs"]["background"] is False


def test_dtd_and_entity_content_is_rejected_before_xml_parse(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.dclg"
    source.write_bytes(
        b'''<?xml version="1.0"?><!DOCTYPE doclang [<!ENTITY x "expanded">]>
<doclang version="0.7"><text>&x;</text></doclang>'''
    )

    with pytest.raises(DocLangAdapterError, match="DTDs or entities"):
        prepare_doclang_contribution(source, validation="structural")


def test_reference_validation_allows_official_empty_namespace_form(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "bare.dclg"
    source.write_bytes(b"<doclang><text>Reusable result</text></doclang>")
    captured = {}

    class ReferenceModule:
        @staticmethod
        def validate(path, **kwargs):
            captured["path"] = path
            captured["kwargs"] = kwargs

    monkeypatch.setattr(
        "rdn.doclang_adapter.importlib.import_module",
        lambda name: ReferenceModule if name == "doclang" else None,
    )
    monkeypatch.setattr(
        "rdn.doclang_adapter._distribution_version",
        lambda name: "0.7.3" if name == "doclang" else None,
    )

    prepared = prepare_doclang_contribution(source, validation="reference")

    assert captured["kwargs"] == {
        "allow_empty_namespace": True,
        "xsd_only": True,
    }
    assert prepared.adapter["validation"] == {
        "status": "valid",
        "method": "reference-xsd",
        "toolkit_version": "0.7.3",
    }


def test_non_doclang_source_uses_injected_docling_converter(tmp_path: Path) -> None:
    source_bytes = b"%PDF-1.7 test fixture"
    source = tmp_path / "source.pdf"
    source.write_bytes(source_bytes)

    class Document:
        @staticmethod
        def export_to_doclang() -> str:
            return VALID_DOCLANG.decode("utf-8")

    class Result:
        document = Document()

    class FakeConverter:
        def __init__(self) -> None:
            self.seen = None

        def convert(self, value):
            self.seen = value
            return Result()

    converter = FakeConverter()
    prepared = prepare_doclang_contribution(
        source,
        converter=converter,
        validation="structural",
    )

    assert converter.seen == source
    assert prepared.content == VALID_DOCLANG
    assert prepared.adapter["source"]["media_type"] == "application/pdf"
    assert prepared.adapter["source"]["digest"]["value"] == hashlib.sha256(
        source_bytes
    ).hexdigest()
    assert prepared.adapter["implementation"]["name"] == __name__.split(".", 1)[0]


def test_adapter_metadata_is_path_independent_for_same_source(tmp_path: Path) -> None:
    first = tmp_path / "one.dclg"
    second_dir = tmp_path / "nested"
    second_dir.mkdir()
    second = second_dir / "two.dclg"
    first.write_bytes(VALID_DOCLANG)
    second.write_bytes(VALID_DOCLANG)

    prepared_a = prepare_doclang_contribution(first, validation="structural")
    prepared_b = prepare_doclang_contribution(second, validation="structural")

    assert prepared_a == prepared_b


def test_source_size_limit_is_enforced_before_conversion(tmp_path: Path) -> None:
    source = tmp_path / "large.pdf"
    source.write_bytes(b"x" * 33)

    with pytest.raises(DocLangAdapterError, match="exceeds"):
        prepare_doclang_contribution(
            source,
            converter=object(),
            validation="structural",
            max_source_bytes=32,
        )


def test_output_size_limit_is_enforced_after_conversion(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"small source")

    class Document:
        @staticmethod
        def export_to_doclang() -> str:
            return VALID_DOCLANG.decode("utf-8")

    class Result:
        document = Document()

    class FakeConverter:
        @staticmethod
        def convert(value):
            return Result()

    with pytest.raises(DocLangAdapterError, match="contribution limit"):
        prepare_doclang_contribution(
            source,
            converter=FakeConverter(),
            validation="structural",
            max_output_bytes=len(VALID_DOCLANG) - 1,
        )
