"""SurveyPersonaSource: the persona data a survey samples from (shared rules).

Both ``SurveyExecutionManager`` (accepting a survey, BR1.2) and
``SurveyRunManager`` (starting it, BR1.4) need the same two decisions, so they
live here (Manager → Component → Service):

- Nemotron is usable only when its Parquet is at ``Config.NEMOTRON_DATASET_KEY``.
  A missing file is never downloaded here: it is ``NEMOTRON_DATASET_NOT_PLACED``
  when the file is placed beforehand (closed network), else
  ``SURVEY_DATASET_NOT_DOWNLOADED`` (closed network U3 BR2.4).
- At least :data:`MIN_SURVEY_PERSONAS` personas must be available. Too few with
  filters suggests loosening them; too few without filters means the dataset
  itself is small (Issue #118).

Services are injected by the caller; errors are raised as the caller's own
exception type so each Manager keeps its error classes.
"""

import logging
from typing import Any, Dict, Optional

from ...config import config
from ...models.errors import CodedError, ErrorCode
from ...models.nemotron_dataset import NemotronSource
from ...models.survey import MIN_SURVEY_PERSONAS

logger = logging.getLogger(__name__)

__all__ = ["MIN_SURVEY_PERSONAS", "SurveyPersonaSource"]


class SurveyPersonaSource:
    """Dataset presence and the persona count rule of surveys."""

    def __init__(self, batch_service: Any, s3_service: Any) -> None:
        """Create the component.

        Args:
            batch_service: ``SurveyBatchService`` (DuckDB on the Parquet).
            s3_service: ``S3Service`` (presence of the Nemotron Parquet).
        """
        self._batch = batch_service
        self._s3 = s3_service

    def ensure_dataset(
        self, datasource: str, error_type: type[CodedError] = CodedError
    ) -> None:
        """Point DuckDB at the Nemotron Parquet once it is known to exist.

        Custom data needs nothing here (its record and file are checked by the
        caller).

        Raises:
            error_type: ``NEMOTRON_DATASET_NOT_PLACED`` or
                ``SURVEY_DATASET_NOT_DOWNLOADED``.
        """
        if datasource.startswith("custom:") or self._batch.has_parquet_uri():
            return
        key = config.NEMOTRON_DATASET_KEY
        path = self._s3.path_of(key)
        try:
            present = bool(self._s3.exists(path))
        except Exception as e:
            # The S3 key is a file path: logged here, never put in the context.
            logger.warning(
                "Nemotron parquet check failed at %s: %s", key, type(e).__name__
            )
            present = False
        if not present:
            logger.warning("Nemotron parquet not usable at %s", key)
            if config.NEMOTRON_SOURCE == NemotronSource.S3:
                raise error_type(
                    "nemotron parquet is not placed",
                    code=ErrorCode.NEMOTRON_DATASET_NOT_PLACED,
                )
            raise error_type(
                "nemotron parquet uri is not configured",
                code=ErrorCode.SURVEY_DATASET_NOT_DOWNLOADED,
            )
        self._batch.set_parquet_s3_uri(path)

    @staticmethod
    def require_enough(
        available: int,
        filters: Optional[Dict[str, Any]],
        error_type: type[CodedError] = CodedError,
    ) -> None:
        """At least :data:`MIN_SURVEY_PERSONAS` personas (BR1.2, BR1.4).

        Raises:
            error_type: ``SURVEY_AVAILABLE_PERSONAS_TOO_FEW`` (filters) or
                ``SURVEY_DATASET_TOO_FEW_ROWS`` (no filters).
        """
        if available >= MIN_SURVEY_PERSONAS:
            return
        if filters:
            raise error_type(
                f"only {available} personas match the filters "
                f"(minimum {MIN_SURVEY_PERSONAS})",
                code=ErrorCode.SURVEY_AVAILABLE_PERSONAS_TOO_FEW,
                context={
                    "available_count": available,
                    "min_count": MIN_SURVEY_PERSONAS,
                },
            )
        raise error_type(
            f"datasource has only {available} personas (minimum {MIN_SURVEY_PERSONAS})",
            code=ErrorCode.SURVEY_DATASET_TOO_FEW_ROWS,
            context={"row_count": available, "min_rows": MIN_SURVEY_PERSONAS},
        )
