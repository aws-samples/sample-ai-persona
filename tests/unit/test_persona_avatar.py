"""Tests for persona avatar images (model, PersonaManager, display helper)."""

from io import BytesIO
from unittest.mock import Mock

import pytest
from PIL import Image

from src.managers.persona_manager import PersonaManager, PersonaManagerError
from src.models.asset_delivery import DeliveryResult, SizeLimit
from src.models.authz import Principal, Role
from src.models.errors import CodedError, ErrorCode
from src.models.persona import Persona
from tests.error_helpers import raises_code
from web.routers._persona_avatar import persona_avatar_url

OLD_PATH = "s3://bucket/persona_avatars/p1/11111111-old.webp"
NEW_PATH = "s3://bucket/persona_avatars/p1/22222222-new.webp"


def _persona(avatar_path: str | None = None) -> Persona:
    persona = Persona.create_new(
        name="テスト",
        age=30,
        occupation="会社員",
        background="背景",
        values=["v"],
        pain_points=["p"],
        goals=["g"],
    )
    return persona.with_avatar(avatar_path).owned_by(OWNER.sub)


#: The persona's owner: avatar changes need manage, the URL needs read
OWNER = Principal("sub-owner", frozenset({Role.MEMBER}))


def _png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 64), (0, 128, 255)).save(buf, format="PNG")
    return buf.getvalue()


class TestPersonaModelAvatar:
    def test_to_dict_omits_none_and_round_trips(self):
        without = _persona()
        assert "avatar_path" not in without.to_dict()

        with_avatar = _persona(OLD_PATH)
        restored = Persona.from_dict(with_avatar.to_dict())
        assert restored.avatar_path == OLD_PATH

    def test_update_keeps_avatar_path(self):
        persona = _persona(OLD_PATH)

        assert persona.update(name="新しい名前").avatar_path == OLD_PATH

    def test_with_avatar_returns_new_instance(self):
        persona = _persona()

        updated = persona.with_avatar(NEW_PATH)

        assert updated is not persona
        assert persona.avatar_path is None
        assert updated.avatar_path == NEW_PATH
        assert updated.id == persona.id


class TestPersonaAvatarUrlHelper:
    def test_returns_versioned_app_url(self):
        persona = _persona(NEW_PATH)

        assert persona_avatar_url(persona) == (
            f"/persona/{persona.id}/avatar?v=22222222-new"
        )

    def test_returns_empty_without_avatar(self):
        assert persona_avatar_url(_persona()) == ""


@pytest.fixture
def db():
    mock = Mock()
    mock.update_avatar.return_value = True
    return mock


@pytest.fixture
def s3():
    mock = Mock()
    mock.upload_file.return_value = NEW_PATH
    mock.generate_presigned_url.return_value = "https://signed.example/x"
    return mock


@pytest.fixture
def manager(db, s3):
    return PersonaManager(persona_store=db, s3_service=s3)


class TestSetAvatar:
    def test_uploads_normalized_webp_and_saves_path(self, manager, db, s3):
        persona = _persona()
        db.get.return_value = persona

        updated = manager.set_avatar(OWNER, persona.id, _png())

        assert updated.avatar_path == NEW_PATH
        content, key = s3.upload_file.call_args.args
        assert key.startswith(f"persona_avatars/{persona.id}/")
        assert key.endswith(".webp")
        assert s3.upload_file.call_args.kwargs["content_type"] == "image/webp"
        with Image.open(BytesIO(content)) as img:
            assert img.format == "WEBP"
        # write only the path, conditioned on the value read at that time (no image)
        db.update_avatar.assert_called_once()
        call = db.update_avatar.call_args
        assert call.args == (persona.id,)
        assert call.kwargs["new_path"] == NEW_PATH
        assert call.kwargs["expected_path"] is None
        db.update_editable.assert_not_called()
        s3.delete_file.assert_not_called()

    def test_replacing_deletes_previous_object(self, manager, db, s3):
        db.get.return_value = _persona(OLD_PATH)

        manager.set_avatar(OWNER, "p1", _png())

        assert db.update_avatar.call_args.kwargs["expected_path"] == OLD_PATH
        s3.delete_file.assert_called_once_with(OLD_PATH)

    def test_db_failure_rolls_back_uploaded_object(self, manager, db, s3):
        db.get.return_value = _persona(OLD_PATH)
        db.update_avatar.side_effect = RuntimeError("boom")

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_UPDATE_FAILED):
            manager.set_avatar(OWNER, "p1", _png())

        s3.delete_file.assert_called_once_with(NEW_PATH)

    def test_persona_deleted_during_upload_is_not_recreated(self, manager, db, s3):
        """The persona was deleted during processing: delete the new image and raise NOT_FOUND."""
        db.get.side_effect = [_persona(OLD_PATH), None]
        db.update_avatar.return_value = False

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_NOT_FOUND):
            manager.set_avatar(OWNER, "p1", _png())

        db.update_editable.assert_not_called()
        db.put_new.assert_not_called()
        s3.delete_file.assert_called_once_with(NEW_PATH)

    def test_concurrent_change_is_conflict(self, manager, db, s3):
        """Beaten by a concurrent upload: delete only our own image and keep the other's and the old one."""
        db.get.side_effect = [_persona(OLD_PATH), _persona("s3://other")]
        db.update_avatar.return_value = False

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_AVATAR_CONFLICT):
            manager.set_avatar(OWNER, "p1", _png())

        s3.delete_file.assert_called_once_with(NEW_PATH)

    def test_invalid_image_is_rejected_before_upload(self, manager, db, s3):
        db.get.return_value = _persona()

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_AVATAR_INVALID_IMAGE):
            manager.set_avatar(OWNER, "p1", b"<svg></svg>")

        s3.upload_file.assert_not_called()

    def test_empty_file_is_rejected(self, manager):
        with raises_code(PersonaManagerError, ErrorCode.FILE_EMPTY):
            manager.set_avatar(OWNER, "p1", b"")

    def test_oversized_file_is_rejected(self, manager, monkeypatch):
        from src.config import config

        monkeypatch.setattr(config, "MAX_IMAGE_SIZE", 10)

        with raises_code(PersonaManagerError, ErrorCode.FILE_TOO_LARGE):
            manager.set_avatar(OWNER, "p1", b"x" * 11)

    def test_missing_persona_is_not_found(self, manager, db):
        db.get.return_value = None

        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.set_avatar(OWNER, "missing", _png())

    def test_upload_failure_is_transient(self, manager, db, s3):
        db.get.return_value = _persona()
        s3.upload_file.side_effect = RuntimeError("s3 down")

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_OPERATION_FAILED):
            manager.set_avatar(OWNER, "p1", _png())

        db.update_editable.assert_not_called()


class TestDeleteAvatar:
    def test_clears_path_and_deletes_object(self, manager, db, s3):
        db.get.return_value = _persona(OLD_PATH)

        updated = manager.delete_avatar(OWNER, "p1")

        assert updated.avatar_path is None
        call = db.update_avatar.call_args
        assert call.kwargs["new_path"] is None
        assert call.kwargs["expected_path"] == OLD_PATH
        s3.delete_file.assert_called_once_with(OLD_PATH)

    def test_noop_without_avatar(self, manager, db, s3):
        db.get.return_value = _persona()

        manager.delete_avatar(OWNER, "p1")

        db.update_avatar.assert_not_called()
        s3.delete_file.assert_not_called()

    def test_db_failure_keeps_object(self, manager, db, s3):
        db.get.return_value = _persona(OLD_PATH)
        db.update_avatar.side_effect = RuntimeError("boom")

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_UPDATE_FAILED):
            manager.delete_avatar(OWNER, "p1")

        s3.delete_file.assert_not_called()

    def test_concurrent_change_keeps_objects(self, manager, db, s3):
        """Replaced by another image during removal: delete neither image."""
        db.get.side_effect = [_persona(OLD_PATH), _persona(NEW_PATH)]
        db.update_avatar.return_value = False

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_AVATAR_CONFLICT):
            manager.delete_avatar(OWNER, "p1")

        s3.delete_file.assert_not_called()


class TestDeliverAvatar:
    """After the read authorization, the avatar handle is passed to AssetDelivery."""

    STRANGER = Principal("sub-stranger", frozenset({Role.MEMBER}))

    @pytest.fixture
    def delivery(self) -> Mock:
        mock = Mock()
        mock.deliver.return_value = DeliveryResult.redirect("https://signed")
        return mock

    @pytest.fixture
    def delivering(self, db, s3, delivery):
        return PersonaManager(persona_store=db, s3_service=s3, asset_delivery=delivery)

    def test_hands_the_avatar_handle_to_asset_delivery(self, delivering, db, delivery):
        persona = _persona(OLD_PATH)
        db.get.return_value = persona

        result = delivering.deliver_avatar(OWNER, persona.id)

        assert result is delivery.deliver.return_value
        handle = delivery.deliver.call_args.args[0]
        assert handle.s3_path == OLD_PATH
        assert handle.content_type == "image/webp"
        assert handle.download_name == f"{persona.id}.webp"
        assert handle.size_limit is SizeLimit.UPLOAD
        assert handle.not_found_code is ErrorCode.PERSONA_AVATAR_NOT_FOUND
        assert handle.presigned_expiration == 3600
        assert handle.check_exists is False

    def test_unreadable_persona_never_reaches_asset_delivery(
        self, delivering, db, delivery
    ):
        db.get.return_value = _persona(OLD_PATH)

        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            delivering.deliver_avatar(self.STRANGER, "p1")
        delivery.deliver.assert_not_called()

    def test_without_avatar_is_not_found(self, delivering, db, delivery):
        db.get.return_value = _persona()

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_AVATAR_NOT_FOUND):
            delivering.deliver_avatar(OWNER, "p1")
        delivery.deliver.assert_not_called()

    def test_default_delivery_signs_with_the_manager_s3(self, manager, db, s3):
        db.get.return_value = _persona(OLD_PATH)

        result = manager.deliver_avatar(OWNER, "p1")

        assert result.presigned_url == "https://signed.example/x"
        s3.generate_presigned_url.assert_called_once_with(OLD_PATH, 3600)

    def test_signing_failure_is_a_persona_operation_failure(self, manager, db, s3):
        db.get.return_value = _persona(OLD_PATH)
        s3.generate_presigned_url.side_effect = RuntimeError("boom")

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_OPERATION_FAILED):
            manager.deliver_avatar(OWNER, "p1")


class TestDeletePersonaCleansUpAvatar:
    """Deletes the image of the body at deletion time (the return value of delete_with_shares)."""

    OWNER = Principal("sub-owner", frozenset({Role.MEMBER}))

    @pytest.fixture
    def sharing(self) -> Mock:
        return Mock()

    @pytest.fixture
    def deleter(self, db, s3, sharing) -> PersonaManager:
        bindings = Mock()
        bindings.list_for_persona.return_value = []
        return PersonaManager(
            persona_store=db,
            s3_service=s3,
            resource_sharing=sharing,
            binding_store=bindings,
            memory_service=None,
        )

    def test_deletes_avatar_of_deleted_record(self, deleter, db, s3, sharing):
        """Deletes the image of the record obtained together with the deletion (does not rely on a prior read)."""
        sharing.delete_with_shares.return_value = {"id": "p1", "avatar_path": OLD_PATH}

        assert deleter.delete_persona(self.OWNER, "p1") is True
        s3.delete_file.assert_called_once_with(OLD_PATH)
        db.get.assert_not_called()

    def test_s3_failure_does_not_fail_persona_deletion(self, deleter, s3, sharing):
        sharing.delete_with_shares.return_value = {"id": "p1", "avatar_path": OLD_PATH}
        s3.delete_file.side_effect = RuntimeError("s3 down")

        assert deleter.delete_persona(self.OWNER, "p1") is True

    def test_not_found_does_not_touch_s3(self, deleter, s3, sharing):
        sharing.delete_with_shares.return_value = None

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_NOT_FOUND):
            deleter.delete_persona(self.OWNER, "p1")
        s3.delete_file.assert_not_called()


class TestAvatarStorageUnavailable:
    @pytest.fixture
    def manager_without_s3(self, db, monkeypatch):
        from src.services.service_factory import service_factory

        def _no_bucket():
            raise RuntimeError("S3_BUCKET_NAME is not set")

        monkeypatch.setattr(service_factory, "get_s3_service", _no_bucket)
        return PersonaManager(persona_store=db)

    def test_upload_is_disabled(self, manager_without_s3):
        assert manager_without_s3.avatar_upload_enabled() is False
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_AVATAR_UNAVAILABLE):
            manager_without_s3.set_avatar(OWNER, "p1", _png())


class TestAvatarPixelLimit:
    def test_too_many_pixels_is_capacity_error(self, manager, db, s3, monkeypatch):
        from src.managers.shared import image_normalize

        monkeypatch.setattr(image_normalize, "MAX_AVATAR_SOURCE_PIXELS", 100)
        db.get.return_value = _persona()

        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_AVATAR_TOO_MANY_PIXELS
        ) as exc_info:
            manager.set_avatar(OWNER, "p1", _png())

        assert exc_info.value.context["max_pixels_10k"] == "0"
        s3.upload_file.assert_not_called()

    def test_message_shows_limit(self):
        from src.managers.persona_manager import PersonaManagerError as Err
        from web.error_messages import user_message_for

        message = user_message_for(
            Err(
                "too big",
                code=ErrorCode.PERSONA_AVATAR_TOO_MANY_PIXELS,
                context={"max_pixels_10k": "1,600"},
            )
        )

        assert "1,600万画素" in message
