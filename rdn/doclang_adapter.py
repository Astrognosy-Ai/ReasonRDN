"""Optional DocLang and Docling bridge for Reason contributions.

DocLang is an interchange representation; Reason supplies stable task identity,
contribution history, convergence, and resolution.  The bridge keeps that
boundary exact:

* existing DocLang XML is retained byte-for-byte;
* other local documents may be converted through an optional Docling install;
* source and output digests, format version, implementation version, and
  validation method are bound into contribution adapter metadata; and
* Reason treats the resulting bytes as opaque content.

Neither ``doclang`` nor ``docling`` is a core dependency.  Callers that only
contribute existing ``.dclg`` files can use the built-in bounded structural
check.  Installing ``doclang`` adds reference-XSD validation; installing
``docling`` adds conversion from PDF, Office, HTML, images, and other formats.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import mimetypes
import re
import tempfile
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

from .contribution import ContributionEnvelope


DOCLANG_ADAPTER_ID = "https://doclang.ai/"
DOCLANG_MEDIA_TYPE = "application/vnd.doclang.document+xml"
DEFAULT_MAX_SOURCE_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 700_000

_VERSION_RE = re.compile(r"^\d+\.\d+$")
_DOCLANG_EXTENSIONS = (".dclg", ".dclg.xml")
_FORBIDDEN_XML_MARKERS = (b"<!doctype", b"<!entity")


class DocLangAdapterError(ValueError):
    """A source cannot be prepared as a bounded DocLang contribution."""


class DocLangDependencyError(DocLangAdapterError):
    """An explicitly requested optional DocLang/Docling dependency is absent."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _distribution_version(name: str) -> Optional[str]:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _read_bounded(path: Path, *, max_bytes: int) -> bytes:
    if max_bytes <= 0:
        raise DocLangAdapterError("max_source_bytes must be positive")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DocLangAdapterError(f"cannot read source document: {path}") from exc
    if not path.is_file():
        raise DocLangAdapterError(f"source document is not a file: {path}")
    if size > max_bytes:
        raise DocLangAdapterError(
            f"source document exceeds the {max_bytes}-byte adapter limit"
        )
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise DocLangAdapterError(f"cannot read source document: {path}") from exc
    if len(value) > max_bytes:
        raise DocLangAdapterError(
            f"source document exceeds the {max_bytes}-byte adapter limit"
        )
    return value


def _doclang_root_and_version(value: bytes) -> str:
    """Perform a bounded, entity-free XML parse and return the DocLang version."""
    if not value:
        raise DocLangAdapterError("DocLang content is empty")
    lowered = value.lower()
    if any(marker in lowered for marker in _FORBIDDEN_XML_MARKERS):
        raise DocLangAdapterError("DocLang content must not contain DTDs or entities")
    try:
        root = ElementTree.fromstring(value)
    except (ElementTree.ParseError, ValueError) as exc:
        raise DocLangAdapterError("DocLang content is not well-formed XML") from exc
    local_name = root.tag.rsplit("}", 1)[-1] if isinstance(root.tag, str) else ""
    if local_name.lower() != "doclang":
        raise DocLangAdapterError("XML root element must be doclang")
    version = str(root.attrib.get("version") or "0.7")
    if not _VERSION_RE.fullmatch(version):
        raise DocLangAdapterError("DocLang version must use MAJOR.MINOR form")
    return version


def _is_doclang_source(path: Path, value: bytes) -> bool:
    lowered_name = path.name.lower()
    if lowered_name.endswith(_DOCLANG_EXTENSIONS):
        return True
    prefix = value[:4096].lower()
    return b"<doclang" in prefix or b":doclang" in prefix


def _reference_validate(value: bytes, *, required: bool) -> Optional[str]:
    """Use the optional DocLang reference XSD and return its toolkit version."""
    try:
        module = importlib.import_module("doclang")
    except ImportError as exc:
        if required:
            raise DocLangDependencyError(
                "reference validation requires the optional 'doclang' package"
            ) from exc
        return None

    validate = getattr(module, "validate", None)
    if not callable(validate):
        if required:
            raise DocLangDependencyError(
                "the installed 'doclang' package does not expose validate()"
            )
        return None

    temporary_path: Optional[Path] = None
    try:
        root = ElementTree.fromstring(value)
        allow_empty_namespace = not (
            isinstance(root.tag, str) and root.tag.startswith("{")
        )
        with tempfile.NamedTemporaryFile(suffix=".dclg", delete=False) as handle:
            handle.write(value)
            temporary_path = Path(handle.name)
        validate(
            temporary_path,
            allow_empty_namespace=allow_empty_namespace,
            xsd_only=True,
        )
    except Exception as exc:
        # Keep dependency/validation errors inside the adapter contract instead
        # of leaking backend-specific exception classes into ReasonRDN callers.
        raise DocLangAdapterError(f"DocLang reference validation failed: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return _distribution_version("doclang") or "unknown"


def _converter_identity(converter: Any) -> Dict[str, str]:
    module_name = converter.__class__.__module__.split(".", 1)[0]
    distribution_name = "docling" if module_name == "docling" else module_name
    return {
        "name": distribution_name or converter.__class__.__name__,
        "version": _distribution_version(distribution_name) or "unknown",
    }


def _convert_with_docling(path: Path, converter: Any = None) -> tuple[bytes, Dict[str, str]]:
    if converter is None:
        try:
            converter_module = importlib.import_module("docling.document_converter")
            converter_type = getattr(converter_module, "DocumentConverter")
        except (ImportError, AttributeError) as exc:
            raise DocLangDependencyError(
                "document conversion requires the optional 'docling' package"
            ) from exc
        converter = converter_type()

    try:
        result = converter.convert(path)
        document = result.document
        exported = document.export_to_doclang()
    except Exception as exc:
        raise DocLangAdapterError(f"Docling conversion failed: {exc}") from exc
    if not isinstance(exported, str):
        raise DocLangAdapterError("Docling export_to_doclang() must return text")
    return exported.encode("utf-8"), _converter_identity(converter)


@dataclass(frozen=True)
class PreparedDocLangContribution:
    """Exact DocLang bytes plus deterministic adapter metadata."""

    content: bytes
    media_type: str
    adapter: Dict[str, Any]

    def envelope(
        self,
        reason_address: str,
        *,
        scope: str = "organization",
        project: str = "astrognosy",
        tags: Optional[tuple[str, ...]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> ContributionEnvelope:
        """Create the ordinary format-neutral Reason contribution envelope."""
        return ContributionEnvelope.create(
            self.content,
            reason_address=reason_address,
            scope=scope,
            media_type=self.media_type,
            project=project,
            tags=tags,
            metadata=metadata,
            adapter=self.adapter,
            created_at=created_at,
        )

    def contribute(
        self,
        client: Any,
        reason_address: str,
        *,
        scope: str = "organization",
        project: str = "astrognosy",
        tags: Optional[tuple[str, ...]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        context: Optional[Mapping[str, Any]] = None,
        background: bool = True,
        flush: bool = False,
    ) -> Dict[str, Any]:
        """Queue this document through an ``RDNClient``-compatible object."""
        return client.contribute(
            self.content,
            reason_address=reason_address,
            scope=scope,
            media_type=self.media_type,
            project=project,
            tags=tags,
            metadata=metadata,
            context=context,
            adapter=self.adapter,
            background=background,
            flush=flush,
        )


def prepare_doclang_contribution(
    source: Union[str, Path],
    *,
    converter: Any = None,
    validation: str = "auto",
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> PreparedDocLangContribution:
    """Prepare one local document as exact DocLang contribution bytes.

    ``validation`` may be ``"auto"`` (reference XSD when installed, otherwise
    the bounded structural check), ``"reference"`` (require the reference
    toolkit), or ``"structural"`` (built-in check only).  Network URLs are not
    accepted; callers choose and fetch source material before contribution.
    """
    if validation not in {"auto", "reference", "structural"}:
        raise DocLangAdapterError(
            "validation must be 'auto', 'reference', or 'structural'"
        )
    if max_output_bytes <= 0:
        raise DocLangAdapterError("max_output_bytes must be positive")
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    source_bytes = _read_bounded(path, max_bytes=max_source_bytes)
    source_digest = _sha256(source_bytes)
    is_doclang_source = _is_doclang_source(path, source_bytes)
    source_media_type = (
        DOCLANG_MEDIA_TYPE
        if is_doclang_source
        else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )

    if is_doclang_source:
        doclang_bytes = source_bytes
        implementation = {
            "name": "doclang-source",
            "version": "exact-bytes",
        }
    else:
        doclang_bytes, implementation = _convert_with_docling(path, converter)

    if len(doclang_bytes) > max_output_bytes:
        raise DocLangAdapterError(
            f"DocLang output exceeds the {max_output_bytes}-byte contribution limit"
        )
    spec_version = _doclang_root_and_version(doclang_bytes)
    toolkit_version = None
    if validation in {"auto", "reference"}:
        toolkit_version = _reference_validate(
            doclang_bytes,
            required=validation == "reference",
        )
    validation_metadata: Dict[str, str] = {
        "status": "valid",
        "method": "reference-xsd" if toolkit_version is not None else "bounded-structural",
    }
    if toolkit_version is not None:
        validation_metadata["toolkit_version"] = toolkit_version

    adapter = {
        "id": DOCLANG_ADAPTER_ID,
        "format": "doclang",
        "spec_version": spec_version,
        "implementation": implementation,
        "source": {
            "media_type": source_media_type,
            "digest": {
                "algorithm": "SHA-256",
                "value": source_digest,
            },
        },
        "output": {
            "media_type": DOCLANG_MEDIA_TYPE,
            "digest": {
                "algorithm": "SHA-256",
                "value": _sha256(doclang_bytes),
            },
        },
        "validation": validation_metadata,
    }
    return PreparedDocLangContribution(
        content=doclang_bytes,
        media_type=DOCLANG_MEDIA_TYPE,
        adapter=adapter,
    )
