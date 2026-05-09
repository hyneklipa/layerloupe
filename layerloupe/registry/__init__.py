"""Docker / OCI Registry client and parsing utilities."""

from layerloupe.registry.annotations import (
    KNOWN_OCI_ANNOTATIONS,
    AnnotationRow,
    KnownAnnotation,
    merge_annotations,
)
from layerloupe.registry.auth import BasicAuth, basic_auth_from_settings
from layerloupe.registry.bearer import (
    BearerAuth,
    BearerChallenge,
    TokenCache,
    infer_scope,
    parse_bearer_challenge,
)
from layerloupe.registry.cache import TTLCache
from layerloupe.registry.client import RegistryClient, RegistryProbe, parse_next_link
from layerloupe.registry.exceptions import (
    RegistryConnectionError,
    RegistryError,
    RegistryHTTPError,
)
from layerloupe.registry.layers import (
    LARGE_LAYER_THRESHOLD,
    LayerRow,
    ParsedInstruction,
    build_layer_rows,
    parse_created_by,
)
from layerloupe.registry.manifests import (
    MANIFEST_ACCEPT_HEADER,
    MANIFEST_ACCEPT_TYPES,
    ManifestKind,
    ManifestResponse,
    MediaType,
    classify_media_type,
)
from layerloupe.registry.models import (
    ContainerConfig,
    Descriptor,
    DockerManifestList,
    DockerManifestV2,
    DockerSchema1Manifest,
    FsLayer,
    HistoryEntry,
    ImageConfig,
    IndexManifestEntry,
    OciImageIndex,
    OciImageManifest,
    Platform,
    RootFS,
    V1HistoryEntry,
    V1Signature,
)
from layerloupe.registry.parser import (
    UnifiedConfig,
    UnifiedLayer,
    UnifiedManifest,
    UnifiedPlatform,
    to_unified,
)
from layerloupe.registry.referrers import (
    KNOWN_ARTIFACT_TYPES,
    KnownArtifactType,
    Referrer,
    parse_referrers,
)

__all__ = [
    "KNOWN_ARTIFACT_TYPES",
    "KNOWN_OCI_ANNOTATIONS",
    "LARGE_LAYER_THRESHOLD",
    "MANIFEST_ACCEPT_HEADER",
    "MANIFEST_ACCEPT_TYPES",
    "AnnotationRow",
    "BasicAuth",
    "BearerAuth",
    "BearerChallenge",
    "ContainerConfig",
    "Descriptor",
    "DockerManifestList",
    "DockerManifestV2",
    "DockerSchema1Manifest",
    "FsLayer",
    "HistoryEntry",
    "ImageConfig",
    "IndexManifestEntry",
    "KnownAnnotation",
    "KnownArtifactType",
    "LayerRow",
    "ManifestKind",
    "ManifestResponse",
    "MediaType",
    "OciImageIndex",
    "OciImageManifest",
    "ParsedInstruction",
    "Platform",
    "Referrer",
    "RegistryClient",
    "RegistryConnectionError",
    "RegistryError",
    "RegistryHTTPError",
    "RegistryProbe",
    "RootFS",
    "TTLCache",
    "TokenCache",
    "UnifiedConfig",
    "UnifiedLayer",
    "UnifiedManifest",
    "UnifiedPlatform",
    "V1HistoryEntry",
    "V1Signature",
    "basic_auth_from_settings",
    "build_layer_rows",
    "classify_media_type",
    "infer_scope",
    "merge_annotations",
    "parse_bearer_challenge",
    "parse_created_by",
    "parse_next_link",
    "parse_referrers",
    "to_unified",
]
