"""U5 の新しい型（entities.md、BR1.6、BR5.1〜BR5.3）。

生成候補（PersonaCandidate・BehaviorDatasetCandidate・CandidateClaim）、紐付け
（PersonaBinding）、アップロードファイル（UploadedFile・AttachResult）、長期記憶の
削除の結果（DeleteAllResult）、Dataset.owner_persona_id を確かめる。
"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from src.models.dataset import Dataset, DatasetColumn
from src.models.memory import DeleteAllResult
from src.models.persona import Persona
from src.models.persona_binding import (
    KB_TARGET_KEY,
    BindingTargetType,
    PersonaBinding,
    dataset_target_key,
)
from src.models.persona_candidate import (
    CLAIM_TIMEOUT_SECONDS,
    MAX_CANDIDATE_ITEM_BYTES,
    BehaviorDatasetCandidate,
    CandidateClaim,
    PersonaCandidate,
)
from src.models.uploaded_file import (
    FILE_TYPES,
    UPLOAD_TTL_SECONDS,
    AttachResult,
    UploadedFile,
    file_id_from_key,
    upload_key,
)

NOW = datetime(2026, 10, 14, 3, 0, 0, tzinfo=timezone.utc)
FILE_ID = "0b0c4f5e-1d2a-4b3c-8d9e-0f1a2b3c4d5e"


def _persona() -> Persona:
    return Persona.create_new(
        name="佐藤 花子",
        age=34,
        occupation="会社員",
        background="東京で働く",
        values=["効率"],
        pain_points=["時間"],
        goals=["成長"],
        tags=["都市"],
    )


# =============================================================================
# PersonaCandidate
# =============================================================================


class TestPersonaCandidate:
    def test_create_from_persona_sets_owner_generation_and_expiry(self):
        persona = _persona()
        candidate = PersonaCandidate.from_persona(
            persona,
            owner_sub="sub-a",
            generation_id="gen-1",
            ttl_seconds=28800,
            now=NOW,
            generation_log=[{"type": "thinking", "content": "考え中"}],
            generation_context={"data_type": "interview"},
        )

        assert candidate.candidate_id == persona.id
        assert candidate.owner_sub == "sub-a"
        assert candidate.generation_id == "gen-1"
        assert candidate.expires_at == int(NOW.timestamp()) + 28800
        assert candidate.persona_payload["name"] == "佐藤 花子"
        # 所有・共有・ID は候補の中身に入れない（保存のとき新しく決める）
        for key in ("id", "owner_sub", "shared_group_ids", "avatar_path"):
            assert key not in candidate.persona_payload

    def test_to_dict_omits_none_and_round_trips(self):
        candidate = PersonaCandidate.from_persona(
            _persona(), owner_sub="sub-a", generation_id="g", ttl_seconds=60, now=NOW
        )
        data = candidate.to_dict()

        assert None not in data.values()
        for key in ("claimed_at", "claim_token", "saved_at", "generation_log"):
            assert key not in data
        assert PersonaCandidate.from_dict(data) == candidate

    def test_claim_marks_are_written_and_read(self):
        candidate = PersonaCandidate.from_persona(
            _persona(), owner_sub="sub-a", generation_id="g", ttl_seconds=60, now=NOW
        )
        claimed = candidate.claimed(token="tok", at=NOW)
        saved = claimed.mark_saved(NOW)

        assert claimed.claim_token == "tok"
        assert claimed.claimed_at == NOW
        assert candidate.claim_token is None  # the original is untouched
        assert PersonaCandidate.from_dict(saved.to_dict()).saved_at == NOW

    def test_expiry_and_stale_claims(self):
        candidate = PersonaCandidate.from_persona(
            _persona(), owner_sub="sub-a", generation_id="g", ttl_seconds=60, now=NOW
        )
        assert not candidate.is_expired(NOW + timedelta(seconds=59))
        assert candidate.is_expired(NOW + timedelta(seconds=60))

        claimed = candidate.claimed(token="t", at=NOW)
        assert claimed.claim_is_active(
            NOW + timedelta(seconds=CLAIM_TIMEOUT_SECONDS - 1)
        )
        assert not claimed.claim_is_active(
            NOW + timedelta(seconds=CLAIM_TIMEOUT_SECONDS)
        )

    def test_to_persona_rebuilds_the_fields_under_a_new_id(self):
        persona = _persona()
        candidate = PersonaCandidate.from_persona(
            persona,
            owner_sub="sub-a",
            generation_id="g",
            ttl_seconds=60,
            now=NOW,
            generation_log=[{"type": "thinking", "content": "x"}],
            generation_context={"persona_count": 1},
        )
        rebuilt = candidate.to_persona()

        assert rebuilt.id != candidate.candidate_id
        assert rebuilt.name == persona.name
        assert rebuilt.tags == ["都市"]
        assert rebuilt.generation_log == [{"type": "thinking", "content": "x"}]
        assert rebuilt.generation_context == {"persona_count": 1}
        assert rebuilt.owner_sub is None

    def test_is_frozen(self):
        candidate = PersonaCandidate.from_persona(
            _persona(), owner_sub="s", generation_id="g", ttl_seconds=60, now=NOW
        )
        with pytest.raises(FrozenInstanceError):
            candidate.owner_sub = "other"  # type: ignore[misc]

    def test_size_limit_is_350kb(self):
        assert MAX_CANDIDATE_ITEM_BYTES == 350 * 1024


class TestBehaviorDatasetCandidate:
    def test_temp_key_shape(self):
        key = BehaviorDatasetCandidate.temp_key("sub-a", "cand-1", "tmp-1")
        assert key == "candidates/sub-a/cand-1/tmp-1.csv"

    def test_round_trip_keeps_columns(self):
        item = BehaviorDatasetCandidate(
            temp_id="tmp-1",
            name="佐藤_購買履歴",
            data_type_label="購買履歴",
            temp_s3_key="candidates/sub-a/c/tmp-1.csv",
            columns=(DatasetColumn(name="user_id", data_type="string"),),
            row_count=3,
            binding_key_column="user_id",
            binding_key_value="U1",
        )
        assert BehaviorDatasetCandidate.from_dict(item.to_dict()) == item

    def test_row_count_must_be_positive(self):
        with pytest.raises(ValueError):
            BehaviorDatasetCandidate(
                temp_id="t",
                name="n",
                data_type_label="l",
                temp_s3_key="candidates/s/c/t.csv",
                columns=(),
                row_count=0,
            )

    def test_optional_binding_key_is_not_serialized(self):
        item = BehaviorDatasetCandidate(
            temp_id="t",
            name="n",
            data_type_label="l",
            temp_s3_key="candidates/s/c/t.csv",
            columns=(),
            row_count=1,
        )
        assert "binding_key_column" not in item.to_dict()


class TestCandidateClaim:
    def test_holds_token_and_candidates_in_order(self):
        a = PersonaCandidate.from_persona(
            _persona(), owner_sub="s", generation_id="g", ttl_seconds=60, now=NOW
        )
        claim = CandidateClaim(token="tok", owner_sub="s", candidates=(a,))
        assert claim.ids == (a.candidate_id,)
        assert claim.get(a.candidate_id) is a
        assert claim.get("missing") is None


# =============================================================================
# PersonaBinding
# =============================================================================


class TestPersonaBinding:
    def test_dataset_binding_key(self):
        binding = PersonaBinding.for_dataset(
            "p1", "d1", binding_keys={"user_id": "U1"}, now=NOW
        )
        assert binding.target_key == "DATASET#d1" == dataset_target_key("d1")
        assert binding.target_type is BindingTargetType.DATASET
        assert binding.target_id == "d1"
        assert not binding.dedicated

    def test_kb_binding_uses_the_fixed_key(self):
        binding = PersonaBinding.for_kb(
            "p1", "kb-9", metadata_filters={"a": "b"}, now=NOW
        )
        assert binding.target_key == KB_TARGET_KEY == "KB"
        assert binding.target_id == "kb-9"
        assert binding.metadata_filters == {"a": "b"}

    def test_round_trip_without_none_or_empty_maps(self):
        binding = PersonaBinding.for_dataset("p1", "d1", now=NOW, dedicated=True)
        data = binding.to_dict()

        assert None not in data.values()
        assert "metadata_filters" not in data
        assert data["target_key"] == "DATASET#d1"
        assert PersonaBinding.from_dict(data) == binding

    def test_unknown_target_type_is_rejected(self):
        with pytest.raises(ValueError):
            PersonaBinding.from_dict(
                {
                    "persona_id": "p",
                    "target_key": "X#1",
                    "target_type": "X",
                    "target_id": "1",
                    "created_at": NOW.isoformat(),
                }
            )


# =============================================================================
# UploadedFile
# =============================================================================


class TestUploadKey:
    def test_key_has_the_owner_and_type(self):
        key = upload_key("sub-a", "discussion_document", FILE_ID, "資料.pdf")
        assert key == f"uploads/sub-a/discussion_document/{FILE_ID}_資料.pdf"

    def test_file_id_comes_back_from_the_key(self):
        key = upload_key("local:a@example.com", "survey_image", FILE_ID, "a_b.png")
        assert file_id_from_key(key) == FILE_ID

    @pytest.mark.parametrize(
        "key",
        [
            "",
            "survey_images/x.png",
            f"uploads/sub/survey_image/{FILE_ID}",  # no name part
            "uploads/sub/survey_image/not-a-uuid_x.png",
            f"uploads/sub/other_type/{FILE_ID}_x.png",
            f"uploads//survey_image/{FILE_ID}_x.png",
            f"datasets/sub/survey_image/{FILE_ID}_x.png",
        ],
    )
    def test_keys_of_another_shape_have_no_file_id(self, key):
        assert file_id_from_key(key) is None


class TestUploadedFile:
    def _file(self, **overrides: object) -> UploadedFile:
        base = UploadedFile.create_new(
            owner_sub="sub-a",
            file_type="discussion_document",
            original_filename="資料.pdf",
            file_size=10,
            file_hash="h",
            mime_type="application/pdf",
            now=NOW,
            file_id=FILE_ID,
        )
        data = {**base.to_dict(), **overrides}
        return UploadedFile.from_dict(data)

    def test_create_sets_key_and_24h_expiry(self):
        f = self._file()
        assert f.s3_key == f"uploads/sub-a/discussion_document/{FILE_ID}_資料.pdf"
        assert f.expires_at == int(NOW.timestamp()) + UPLOAD_TTL_SECONDS
        assert UPLOAD_TTL_SECONDS == 24 * 3600
        assert not f.is_attached

    def test_parent_type_and_id_come_together(self):
        with pytest.raises(ValueError):
            self._file(parent_type="discussion")
        with pytest.raises(ValueError):
            self._file(parent_id="d1")

    def test_only_two_file_types(self):
        assert FILE_TYPES == ("discussion_document", "survey_image")
        with pytest.raises(ValueError):
            self._file(file_type="knowledge_file")

    def test_unknown_parent_type_is_rejected(self):
        with pytest.raises(ValueError):
            self._file(parent_type="interview", parent_id="i1")

    def test_attached_file_has_no_expiry_and_round_trips(self):
        f = self._file().attached_to("discussion", "d1")
        data = f.to_dict()

        assert f.is_attached
        assert "expires_at" not in data
        assert None not in data.values()
        assert UploadedFile.from_dict(data) == f

    def test_usable_before_attach_only_by_owner_before_expiry(self):
        f = self._file()
        now_epoch = int(NOW.timestamp())
        assert f.usable_before_attach("sub-a", now_epoch)
        assert not f.usable_before_attach("sub-b", now_epoch)
        assert not f.usable_before_attach("sub-a", f.expires_at or 0)
        assert not f.attached_to("discussion", "d").usable_before_attach(
            "sub-a", now_epoch
        )


class TestAttachResult:
    def test_keeps_the_original_expiry_per_file(self):
        result = AttachResult(
            parent_type="discussion",
            parent_id="d1",
            original_expires_at={"f1": 100, "f2": 200},
        )
        assert result.file_ids == ("f1", "f2")
        assert AttachResult.empty("discussion", "d1").file_ids == ()


# =============================================================================
# DeleteAllResult / Dataset
# =============================================================================


class TestDeleteAllResult:
    def test_complete_only_when_nothing_is_left(self):
        assert DeleteAllResult(deleted=3, remaining=0, timed_out=False).complete
        assert not DeleteAllResult(deleted=3, remaining=None, timed_out=True).complete
        assert not DeleteAllResult(
            deleted=0, remaining=None, timed_out=False, error_type="ClientError"
        ).complete

    def test_disabled_result_is_complete(self):
        assert DeleteAllResult.nothing().complete


class TestDatasetOwnerPersona:
    def _dataset(self, **kwargs: object) -> Dataset:
        return Dataset.create_new(
            name="d", description="", s3_path="s3://b/k", columns=[], **kwargs
        )

    def test_org_asset_has_no_owner_persona(self):
        data = self._dataset().to_dict()
        assert "owner_persona_id" not in data
        assert Dataset.from_dict(data).owner_persona_id is None

    def test_dedicated_dataset_round_trips(self):
        dataset = self._dataset(owner_persona_id="p1")
        assert Dataset.from_dict(dataset.to_dict()).owner_persona_id == "p1"
        assert dataset.is_dedicated_to("p1")
        assert not dataset.is_dedicated_to("p2")
