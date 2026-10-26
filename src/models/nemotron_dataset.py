"""
Nemotron dataset source, key rules and status (closed network U3).

``NEMOTRON_SOURCE`` decides where the Nemotron Parquet comes from: downloaded
from Hugging Face by an admin (``huggingface``, the public default) or placed
in the app bucket beforehand (``s3``, the closed network). The key rules below
are the same as the CDK ``DeploymentProfile`` check (BR5.3) so that a value
given by hand also stops the app at start-up (BR1.4).

Standard library only (Models layer).
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Optional


class NemotronSource(StrEnum):
    """Where the Nemotron Parquet comes from (``NEMOTRON_SOURCE``, BR1.1)."""

    HUGGINGFACE = "huggingface"
    S3 = "s3"


#: The key the app has always used (BR1.2).
DEFAULT_NEMOTRON_DATASET_KEY = (
    "persona-dataset/nemotron-personas-japan.parquet"  # gitleaks:allow
)
#: Places of other files in the bucket (custom persona data, import temp files).
RESERVED_KEY_PREFIXES: tuple[str, ...] = (
    "persona-dataset/custom/",
    "persona-dataset/temp/",
)
#: The ASCII part of the runtime S3 URI check (``SurveyBatchService``).
_KEY_CHARACTERS = re.compile(r"[A-Za-z0-9._\-/]+")
_BYTES_PER_MB = 1024 * 1024


def nemotron_dataset_key_problem(key: str) -> Optional[str]:
    """Why ``key`` cannot be the Nemotron Parquet key, or ``None`` when it can.

    Args:
        key: A key inside the app bucket.

    Returns:
        A short English reason (for start-up errors and logs), or ``None``.
    """
    if not _KEY_CHARACTERS.fullmatch(key):
        return "only letters, digits, '.', '_', '-' and '/' are allowed"
    if key.startswith("/"):
        return "must not start with '/'"
    if "//" in key:
        return "must not contain '//'"
    if ".." in key.split("/"):
        return "must not contain a '..' segment"
    if not key.endswith(".parquet"):
        return "must end with '.parquet'"
    if key.startswith(RESERVED_KEY_PREFIXES):
        return "must not be under " + " or ".join(RESERVED_KEY_PREFIXES)
    return None


@dataclass(frozen=True)
class NemotronDatasetStatus:
    """What the persona data screen shows about the Nemotron file (BR2.2).

    Holds no user data. ``download_allowed`` follows from ``source``: only the
    Hugging Face source offers the download button (BR4.1).
    """

    source: NemotronSource
    dataset_key: str
    available: bool
    size_mb: float = 0

    @property
    def download_allowed(self) -> bool:
        """Whether the screen may offer a download from Hugging Face."""
        return self.source is NemotronSource.HUGGINGFACE

    @classmethod
    def placed(
        cls, source: NemotronSource, dataset_key: str, size_bytes: int
    ) -> "NemotronDatasetStatus":
        """The file is at ``dataset_key``; its size is shown in MB (one decimal)."""
        return cls(
            source=source,
            dataset_key=dataset_key,
            available=True,
            size_mb=round(size_bytes / _BYTES_PER_MB, 1),
        )

    @classmethod
    def missing(
        cls, source: NemotronSource, dataset_key: str
    ) -> "NemotronDatasetStatus":
        """No file is at ``dataset_key`` (or it could not be checked)."""
        return cls(source=source, dataset_key=dataset_key, available=False)

    def to_dict(self) -> dict[str, Any]:
        """Plain values for templates and JSON."""
        return {
            "source": self.source.value,
            "dataset_key": self.dataset_key,
            "available": self.available,
            "size_mb": self.size_mb,
            "download_allowed": self.download_allowed,
        }
