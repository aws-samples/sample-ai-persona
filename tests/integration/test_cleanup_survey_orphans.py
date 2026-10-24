"""後始末で残った S3 のオブジェクトを見つけるスクリプトの結合テスト（moto）。

NFR1.84、NFR1.85。アンケートの削除は本体と共有を先に消し、結果の CSV を 3 回まで
消し直す。カスタムデータの削除は登録情報を先に消し、Parquet を 3 回まで消し直す。
それでも残った ``survey-results/{id}/`` と ``persona-dataset/custom/{id}.parquet`` を
このスクリプトが見つける。既定は件数を出すだけで何も消さない。--apply で消す。
作成から 1 時間以内のものは、保存の途中かもしれないため対象にしない。
"""

import time
import uuid
from typing import Any


from scripts.cleanup_survey_orphans import apply_plan, main, plan_cleanup
from tests.unit.survey_world import SurveyWorld

#: The script runs two hours after the objects were written (outside the grace).
LATER = time.time() + 2 * 3600
#: Thirty minutes after: still inside the one-hour grace.
SOON = time.time() + 30 * 60


def _keys(world: SurveyWorld, prefix: str = "") -> list[str]:
    listed = world.s3.s3_client.list_objects_v2(
        Bucket=world.s3.bucket_name, Prefix=prefix
    )
    return sorted(o["Key"] for o in listed.get("Contents", []))


def _orphan_result(world: SurveyWorld) -> str:
    survey_id = str(uuid.uuid4())
    world.s3.upload_file(b"csv", f"survey-results/{survey_id}/results.csv")
    world.s3.upload_file(b"jsonl", f"survey-results/{survey_id}/output.jsonl")
    return survey_id


def _orphan_custom(world: SurveyWorld) -> str:
    data_id = str(uuid.uuid4())
    world.s3.upload_file(b"parquet", f"persona-dataset/custom/{data_id}.parquet")
    return data_id


def _run(world: SurveyWorld, *argv: str, now: float = LATER) -> int:
    return main(
        list(argv),
        surveys=world.survey_store,
        custom_data=world.custom_store,
        objects=world.s3,
        now=now,
    )


class TestPlan:
    def test_counts_results_without_a_survey_and_files_without_a_record(
        self, survey_world: SurveyWorld
    ):
        alive = survey_world.survey("a")
        record = survey_world.custom_data("使っているデータ")
        orphan_survey = _orphan_result(survey_world)
        orphan_custom = _orphan_custom(survey_world)

        plan = plan_cleanup(
            survey_world.survey_store,
            survey_world.custom_store,
            survey_world.s3,
            now=LATER,
        )

        assert plan.results == {orphan_survey: 2}
        assert plan.custom_files == [f"persona-dataset/custom/{orphan_custom}.parquet"]
        assert alive.id not in plan.results
        assert record.s3_key not in plan.custom_files
        assert not plan.is_clean

    def test_recent_objects_are_left_alone(self, survey_world: SurveyWorld):
        survey_id = _orphan_result(survey_world)
        data_id = _orphan_custom(survey_world)

        plan = plan_cleanup(
            survey_world.survey_store,
            survey_world.custom_store,
            survey_world.s3,
            now=SOON,
        )

        assert plan.results == {} and plan.custom_files == []
        assert plan.recent == sorted(
            [
                f"survey-results/{survey_id}/",
                f"persona-dataset/custom/{data_id}.parquet",
            ]
        )
        assert plan.is_clean

    def test_files_not_named_by_an_id_are_not_touched(self, survey_world: SurveyWorld):
        """v1.0.0 より前の名前のファイル（persona-dataset/custom/{名前}.parquet）は対象外"""
        survey_world.s3.upload_file(b"old", "persona-dataset/custom/顧客データ.parquet")
        survey_world.s3.upload_file(
            b"old", "persona-dataset/custom/顧客データ_meta.json"
        )
        survey_world.s3.upload_file(b"tmp", "persona-dataset/temp/x.csv")

        plan = plan_cleanup(
            survey_world.survey_store,
            survey_world.custom_store,
            survey_world.s3,
            now=LATER,
        )

        assert plan.is_clean
        assert plan.skipped == 2


class TestMain:
    def test_default_only_prints_the_counts(self, survey_world: SurveyWorld, capsys):
        _orphan_result(survey_world)
        _orphan_custom(survey_world)
        before = _keys(survey_world)

        assert _run(survey_world) == 0

        out = capsys.readouterr().out
        assert "本体のない結果: 1 件（2 オブジェクト）" in out
        assert "登録情報のないカスタムデータ: 1 件" in out
        assert "--apply" in out
        assert _keys(survey_world) == before

    def test_apply_deletes_only_the_orphans(self, survey_world: SurveyWorld, capsys):
        alive = survey_world.survey("a")
        record = survey_world.custom_data("使っているデータ")
        _orphan_result(survey_world)
        _orphan_custom(survey_world)

        assert _run(survey_world, "--apply") == 0

        assert _keys(survey_world, "survey-results/") == [
            f"survey-results/{alive.id}/results.csv"
        ]
        assert _keys(survey_world, "persona-dataset/custom/") == [record.s3_key]
        assert "消しました" in capsys.readouterr().out

    def test_clean_bucket(self, survey_world: SurveyWorld, capsys):
        survey_world.survey("a")
        assert _run(survey_world) == 0
        assert "残っているものはありません" in capsys.readouterr().out


class TestApply:
    def test_a_body_that_appeared_meanwhile_is_kept(self, survey_world: SurveyWorld):
        """消す直前にもう一度本体を確かめる（保存が終わったものは消さない）"""
        survey_id = _orphan_result(survey_world)
        data_id = _orphan_custom(survey_world)
        plan = plan_cleanup(
            survey_world.survey_store,
            survey_world.custom_store,
            survey_world.s3,
            now=LATER,
        )

        appeared: Any = {"survey": {survey_id}, "custom": {data_id}}

        class Surveys:
            def get_many(self, ids):
                return {i: object() for i in ids if i in appeared["survey"]}

        class Records:
            def get_many(self, ids):
                return {i: object() for i in ids if i in appeared["custom"]}

        kept = apply_plan(Surveys(), Records(), survey_world.s3, plan)

        assert kept == sorted([survey_id, data_id])
        assert len(_keys(survey_world, f"survey-results/{survey_id}/")) == 2
        assert _keys(survey_world, "persona-dataset/custom/") == [
            f"persona-dataset/custom/{data_id}.parquet"
        ]
