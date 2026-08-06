"""Parser for Artifactory's ``binarystore.xml``.

Artifactory records which binary provider backs its filestore in
``$JFROG_HOME/artifactory/var/etc/artifactory/binarystore.xml``. As a sidecar
with the artifactory volume mounted, airlift can read that file directly and
work out where the blobs actually live, rather than making the operator restate
it. Each side reads its own instance's file, so a sender on Azure Blob and a
receiver on an S3-compatible store need no extra wiring.

Only the topology is taken from the XML. The ``<identity>`` and
``<credential>`` elements are normally stored encrypted by Artifactory (an
opaque ``<keyId>.<algorithm>.<ciphertext>`` envelope), so credentials come from
airlift's own settings instead; see ``_is_encrypted``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from xml.etree import ElementTree

from .. import log

logger = log.get("binarystore.config")

# Default key prefix used by the object-storage providers when <path> is
# omitted. Artifactory writes blobs to "<path>/<sha1[:2]>/<sha1>".
_DEFAULT_PREFIX = "filestore"

# Provider types we can address directly, grouped by the backend that serves
# them. The "-direct" and "cluster-" variants differ only in how Artifactory
# itself streams bytes; the on-disk key layout is identical, so airlift treats
# them the same.
_S3_TYPES = frozenset(
    {
        "s3-storage-v3",
        "s3-storage-v3-direct",
        "cluster-s3-storage-v3",
    }
)
_AZURE_TYPES = frozenset(
    {
        "azure-blob-storage",
        "azure-blob-storage-direct",
        "cluster-azure-blob-storage",
        # JFrog's newer Azure provider. Same element names and the same
        # "<path>/<sha1[:2]>/<sha1>" key layout; only Artifactory's own client
        # implementation differs.
        "azure-blob-storage-v2",
        "azure-blob-storage-v2-direct",
        "cluster-azure-blob-storage-v2",
    }
)
_FILESYSTEM_TYPES = frozenset({"file-system", "cache-fs"})

# Wrapper providers that decorate a single delegate without changing the key
# layout. Walking through them lands on the provider that actually holds the
# bytes.
_PASSTHROUGH_TYPES = frozenset({"cache-fs", "eventual", "retry", "tracking"})

# Artifactory allows either <chain template="s3-storage-v3"/> or an explicit
# nested <chain>. A template names the leaf provider it expands to, so any
# provider type airlift can address is also a template it can accept. Deriving
# the set from the type sets above keeps the two from drifting apart; templates
# outside it (sharded, redundant, database-backed) are refused.
_LEAF_TEMPLATES = _S3_TYPES | _AZURE_TYPES | _FILESYSTEM_TYPES

# Artifactory encrypts secrets in place as "<keyId>.<algorithm>.<ciphertext>",
# e.g. "a1b2c3.aesgcm256.<ciphertext>". Decrypting needs the instance master
# key and an undocumented envelope format, so such values are treated as absent
# and credentials are taken from airlift's settings instead.
_ENCRYPTED_RE = re.compile(r"^[A-Za-z0-9]+\.[A-Za-z0-9]+\.[A-Za-z0-9+/_=-]+$")


class UnsupportedBinarystore(RuntimeError):
    """Raised for a provider chain airlift cannot address safely."""


@dataclass(frozen=True, slots=True)
class FilesystemConfig:
    kind: ClassVar[str] = "filesystem"

    root: Path | None = None


@dataclass(frozen=True, slots=True)
class S3Config:
    kind: ClassVar[str] = "s3"

    bucket: str
    endpoint_url: str
    prefix: str = _DEFAULT_PREFIX
    region: str = "us-east-1"
    path_style: bool = False
    # Populated only when the XML holds usable plaintext credentials. Empty
    # when Artifactory has encrypted them in place; see _is_encrypted.
    access_key: str = ""
    secret_key: str = ""


@dataclass(frozen=True, slots=True)
class AzureConfig:
    kind: ClassVar[str] = "azure"

    container: str
    endpoint_url: str
    account: str = ""
    prefix: str = _DEFAULT_PREFIX
    account_key: str = ""
    # True when Artifactory authenticates to the store with the platform's own
    # instance/managed identity rather than an account key. Airlift signs with
    # SharedKey, so this only serves a clearer error; see binarystore._build.
    instance_credentials: bool = False


BinarystoreConfig = FilesystemConfig | S3Config | AzureConfig


def _is_encrypted(value: str) -> bool:
    """True when a value looks like an Artifactory-encrypted secret."""
    return bool(value) and value.count(".") >= 2 and bool(_ENCRYPTED_RE.match(value))


def _plaintext(settings: ElementTree.Element | None, tag: str) -> str:
    """Return a credential from the XML, but only if it is actually usable.

    Artifactory rewrites secrets in place as an opaque
    "<keyId>.<algorithm>.<ciphertext>" envelope once it has taken ownership of
    the config, so the value on the instance's disk is normally undecryptable
    without the master key. The copy rendered by the Helm chart (before
    Artifactory has touched it) still holds the real value, and mounting that
    is the tidiest way to configure airlift: one source for both topology and
    credentials. So take the value when it is plaintext, and treat the
    encrypted form as absent.
    """
    value = _text(settings, tag)
    if not value or _is_encrypted(value):
        return ""
    return value


def _note_credentials(settings: ElementTree.Element | None, *tags: str) -> None:
    """Log once when the XML's credentials are encrypted and so unusable.

    Operators reasonably expect that pointing airlift at binarystore.xml is
    enough. Saying so explicitly turns a confusing "missing credentials"
    error into an obvious next step.
    """
    if any(_is_encrypted(_text(settings, tag)) for tag in tags):
        logger.info("binarystore.credentials_encrypted")


def _text(element: ElementTree.Element | None, tag: str, default: str = "") -> str:
    if element is None:
        return default
    found = element.find(tag)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _first_text(
    element: ElementTree.Element | None, *tags: str, default: str = ""
) -> str:
    """Return the first of several alternative tags that carries a value."""
    for tag in tags:
        value = _text(element, tag)
        if value:
            return value
    return default


def _flag(element: ElementTree.Element | None, tag: str, default: bool = False) -> bool:
    raw = _text(element, tag)
    if not raw:
        return default
    return raw.strip().lower() == "true"


def _leaf_of_chain(chain: ElementTree.Element) -> tuple[str, ElementTree.Element | None]:
    """Walk a nested <chain> down to the provider that holds the bytes.

    Returns the leaf provider type and the element it was found on (which may
    carry inline settings). Wrapper providers such as cache-fs and eventual
    decorate exactly one delegate, so a branch with more than one child
    provider means a sharding/redundancy chain we deliberately refuse.
    """
    template = chain.get("template", "").strip()
    if template:
        if template not in _LEAF_TEMPLATES:
            raise UnsupportedBinarystore(
                f"unsupported binarystore chain template: {template!r}. "
                "Sharded and database-backed chains are not supported."
            )
        return template, None

    current = chain
    element: ElementTree.Element | None = None
    while True:
        children = current.findall("provider")
        if not children:
            break
        if len(children) > 1:
            types = ", ".join(sorted(c.get("type", "?") for c in children))
            raise UnsupportedBinarystore(
                f"binarystore chain branches into multiple providers ({types}). "
                "Sharded and redundant chains are not supported."
            )
        element = children[0]
        current = element

    if element is None:
        raise UnsupportedBinarystore("binarystore chain declares no provider")

    provider_type = element.get("type", "").strip()
    if not provider_type:
        raise UnsupportedBinarystore("binarystore chain provider has no type attribute")
    return provider_type, element


def _resolve_settings(
    root: ElementTree.Element,
    provider_type: str,
    inline: ElementTree.Element | None,
) -> ElementTree.Element | None:
    """Find the element carrying a provider's settings.

    Artifactory usually declares the chain with empty provider elements and
    puts the settings in sibling top-level <provider> blocks, matched by id or
    type. Some configs inline the settings in the chain instead, so an inline
    element with children wins.

    A template-driven chain names a variant ("...-direct", "cluster-...") whose
    settings block is sometimes declared under the plain provider type of the
    same family, so fall back to any provider from that family rather than
    reporting the settings as missing.
    """
    if inline is not None and len(inline):
        return inline

    # Object-storage families only: a cache-fs block carries a cache directory,
    # not the filestore, so it must never stand in for a file-system provider.
    family = next(
        (f for f in (_S3_TYPES, _AZURE_TYPES) if provider_type in f),
        frozenset(),
    )
    wanted_id = inline.get("id", "").strip() if inline is not None else ""
    by_type: ElementTree.Element | None = None
    by_family: ElementTree.Element | None = None
    for candidate in root.findall("provider"):
        candidate_type = candidate.get("type", "").strip()
        if wanted_id and candidate.get("id", "").strip() == wanted_id:
            return candidate
        if by_type is None and candidate_type == provider_type:
            by_type = candidate
        if by_family is None and candidate_type in family:
            by_family = candidate
    return by_type if by_type is not None else by_family


def _s3_endpoint(settings: ElementTree.Element | None) -> str:
    """Compose the endpoint URL from host, port and the useHttp flag.

    Artifactory stores a bare host in <endpoint> (observed:
    "minio.minio.svc.cluster.local") with the scheme carried separately by
    <useHttp> and the port by <port>. Full URLs are also accepted, since some
    deployments write one.
    """
    host = _text(settings, "endpoint")
    if not host:
        raise UnsupportedBinarystore("S3 provider has no <endpoint>")
    port = _text(settings, "port")
    use_http = _flag(settings, "useHttp")

    if host.startswith(("http://", "https://")):
        base = host.rstrip("/")
    else:
        scheme = "http" if use_http else "https"
        base = f"{scheme}://{host.rstrip('/')}"

    if port and not re.search(r":\d+$", base):
        base = f"{base}:{port}"
    return base


def _azure_endpoint(settings: ElementTree.Element | None, account: str) -> str:
    endpoint = _text(settings, "endpoint")
    if endpoint:
        return endpoint.rstrip("/")
    if not account:
        raise UnsupportedBinarystore(
            "Azure provider has neither <endpoint> nor <accountName>"
        )
    return f"https://{account}.blob.core.windows.net"


def parse(path: Path) -> BinarystoreConfig | None:
    """Parse ``binarystore.xml``, returning None when it is not usable.

    A missing or unparseable file returns None so the caller can fall back to
    the configured filestore root, which is how every pre-object-storage
    deployment behaves. An understood file describing a chain we cannot
    address raises :class:`UnsupportedBinarystore`, because silently guessing a
    key layout would write blobs where Artifactory will never look for them.
    """
    if not path.is_file():
        logger.debug("binarystore.config_absent", path=str(path))
        return None
    try:
        root = ElementTree.fromstring(path.read_text())
    except (OSError, ElementTree.ParseError) as exc:
        logger.warning("binarystore.config_unreadable", path=str(path), error=str(exc))
        return None

    chain = root.find("chain")
    if chain is None:
        logger.warning("binarystore.config_no_chain", path=str(path))
        return None

    provider_type, inline = _leaf_of_chain(chain)
    settings = _resolve_settings(root, provider_type, inline)

    if provider_type in _S3_TYPES:
        bucket = _text(settings, "bucketName")
        if not bucket:
            raise UnsupportedBinarystore("S3 provider has no <bucketName>")
        _note_credentials(settings, "identity", "credential")
        return S3Config(
            bucket=bucket,
            endpoint_url=_s3_endpoint(settings),
            prefix=_text(settings, "path", _DEFAULT_PREFIX).strip("/"),
            region=_text(settings, "region", "us-east-1"),
            path_style=_flag(settings, "enablePathStyleAccess"),
            access_key=_plaintext(settings, "identity"),
            secret_key=_plaintext(settings, "credential"),
        )

    if provider_type in _AZURE_TYPES:
        # The v2 provider names the container <container>; the original names
        # it <containerName>. Both spellings appear in live configs.
        container = _first_text(settings, "containerName", "container")
        if not container:
            raise UnsupportedBinarystore(
                "Azure provider has no <containerName> or <container>"
            )
        _note_credentials(settings, "accountKey")
        account = _text(settings, "accountName")
        return AzureConfig(
            container=container,
            endpoint_url=_azure_endpoint(settings, account),
            account=account,
            prefix=_text(settings, "path", _DEFAULT_PREFIX).strip("/"),
            account_key=_plaintext(settings, "accountKey"),
            instance_credentials=_flag(settings, "useInstanceCredentials"),
        )

    if provider_type in _FILESYSTEM_TYPES:
        # A cache-fs leaf with nothing beneath it is just a local directory.
        # <fileStoreDir> is relative to Artifactory's data dir when not
        # absolute, so leave it to the caller's configured filestore_root
        # unless an absolute path is given.
        raw = _text(settings, "fileStoreDir")
        root_path = Path(raw) if raw.startswith("/") else None
        return FilesystemConfig(root=root_path)

    if provider_type in _PASSTHROUGH_TYPES:
        # A wrapper with no delegate cannot serve bytes on its own.
        raise UnsupportedBinarystore(
            f"binarystore chain ends at wrapper provider {provider_type!r} "
            "with no underlying store"
        )

    raise UnsupportedBinarystore(
        f"unsupported binarystore provider type: {provider_type!r}"
    )
