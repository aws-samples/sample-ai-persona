"""Real U7 Managers behind the routers, on the moto world of tests/unit/survey_world.py.

The API tests sign the client in as one of the sample users of
``tests/conftest.py`` and patch the routers' ``get_*_manager`` functions with
Managers wired to one :class:`SurveyWorld`. Decisions are never mocked:
``AccessPolicy``, ``ResourceSharing``, ``ResourceLookup`` and
``DepartmentDirectory`` are the real ones, on moto DynamoDB and S3.

- :data:`ME` is ``MEMBER_A`` (``sales``); :data:`OTHER` is ``MEMBER_B`` (``dev``,
  unrelated to ME); :data:`ADMIN_USER` is the admin.
- ``b`` is another member of ``sales`` (owner of what is shared with ME).
"""

from contextlib import ExitStack
from typing import Any
from unittest.mock import Mock, patch

import polars as pl

from src.managers.dataset_manager import DatasetManager
from src.managers.settings_manager import SettingsManager
from src.managers.sharing_manager import SharingManager
from src.managers.survey_analysis_manager import SurveyAnalysisManager
from src.managers.survey_dataset_manager import SurveyDatasetManager
from src.managers.survey_execution_manager import SurveyExecutionManager
from src.managers.survey_template_manager import SurveyTemplateManager
from src.managers.shared.result_csv_cache import ResultCsvCache
from tests.conftest import ADMIN, MEMBER_A, MEMBER_B
from tests.unit.survey_world import SurveyWorld

ME = MEMBER_A
OTHER = MEMBER_B
ADMIN_USER = ADMIN
#: Another member of ``sales`` (owns what is shared with ME).
PEER_SUB = "b"
ROWS = 120


def _batch() -> Mock:
    batch = Mock()
    batch.has_parquet_uri.return_value = True
    batch.get_filtered_count.return_value = 500
    batch.get_total_count.return_value = 800
    batch.get_available_filter_values.return_value = {}
    batch.get_preview_stats.return_value = {}
    batch.convert_csv_to_parquet.side_effect = lambda csv, mapping=None: (
        pl.DataFrame({"persona": [f"p{i}" for i in range(ROWS)]}),
        b"parquet",
    )
    batch.generate_statistical_summary.return_value = {}
    return batch


class ApiWorld:
    """The U7 Managers on one :class:`SurveyWorld`."""

    def __init__(self, world: SurveyWorld) -> None:
        self.world = world
        self.batch = _batch()
        self.ai = Mock()
        self.ai.generate_standard_report_streaming.side_effect = lambda **kw: iter(
            ["# レポート"]
        )
        self.cache = ResultCsvCache()
        #: The Step Functions runner and the survey runner loop (no real AWS)
        self.runner = Mock()
        self.runner_configured = True
        self.runner_loop = Mock()
        self.templates = SurveyTemplateManager(
            survey_template_store=world.template_store,
            ai_service=self.ai,
            file_attachments=world.files,
            resource_sharing=world.sharing,
            owners=world.owners,
            resource_lookup=world.lookup,
            sleep=lambda _s: None,
        )
        self.executions = SurveyExecutionManager(
            survey_store=world.survey_store,
            survey_template_store=world.template_store,
            survey_batch_service=self.batch,
            s3_service=world.s3,
            resource_sharing=world.sharing,
            owners=world.owners,
            resource_lookup=world.lookup,
            custom_persona_data_store=world.custom_store,
            result_cache=self.cache,
            sleep=lambda _s: None,
            runner=self.runner,
            runner_configured=lambda: self.runner_configured,
        )
        self.analysis = SurveyAnalysisManager(
            survey_store=world.survey_store,
            survey_template_store=world.template_store,
            s3_service=world.s3,
            ai_service=self.ai,
            survey_batch_service=self.batch,
            resource_lookup=world.lookup,
            resource_sharing=world.sharing,
            result_cache=self.cache,
        )
        self.datasets = SurveyDatasetManager(
            survey_batch_service=self.batch,
            agent_service=Mock(),
            ai_service=self.ai,
            s3_service=world.s3,
            custom_persona_data_store=world.custom_store,
            resource_lookup=world.lookup,
            departments=world.departments,
            sleep=lambda _s: None,
        )
        self.settings_datasets = DatasetManager(
            survey_batch_service=self.batch,
            dataset_store=world.dataset_store,
            s3_service=world.s3,
            departments=world.departments,
            binding_store=world.binding_store,
        )
        self.settings = SettingsManager(
            knowledge_base_store=world.kb_store,
            departments=world.departments,
            binding_store=world.binding_store,
        )
        self.sharing = SharingManager(world.sharing)
        self.file_manager = Mock()
        # 閉域対応 U4: 検査に通ったパスには、アプリのプレビューのルートを埋め込む
        self.file_manager.check_preview_path.return_value = None

    def patch_routers(self, stack: ExitStack) -> None:
        for target, value in [
            ("web.routers.survey.get_template_manager", self.templates),
            ("web.routers.survey.get_execution_manager", self.executions),
            ("web.routers.survey.get_analysis_manager", self.analysis),
            ("web.routers.survey.get_dataset_manager", self.datasets),
            ("web.routers.survey.get_file_manager", self.file_manager),
            ("web.routers.survey.get_survey_runner_loop", self.runner_loop),
            ("web.routers.settings.get_dataset_manager", self.settings_datasets),
            ("web.routers.settings.get_settings_manager", self.settings),
            ("web.routers.sharing.get_sharing_manager", self.sharing),
        ]:
            stack.enter_context(patch(target, return_value=value))

    # --- data -------------------------------------------------------------------

    def template(
        self, owner: str | None = ME.sub, name: str = "テンプレート", **kw: Any
    ):
        return self.world.template(owner, name, **kw)

    def survey(self, owner: str | None = ME.sub, **kw: Any):
        return self.world.survey(owner, **kw)
