"""FileAttachments（ファイルの親への紐付けと配信の判定、U5）の単体テスト。

security-design.md §3 の手順 1〜7、detach、can_read_file、register_parent。
UploadedFileStore と S3Service は moto で本物を使い、AccessPolicy も本物を使う
（判定をモックにしない）。並行の順序は、S3 のタグの操作とトランザクションに差し込みの
関数を持たせたラッパーで作る（スレッドや time.sleep に頼らない）。

BR5.3〜BR5.7、NFR1.27、NFR1.29、NFR1.34、NFR1.37〜NFR1.39、NFR8.15。
"""

from datetime import datetime, timezone
from typing import Any, Callable
from unittest.mock import Mock

import pytest

moto = pytest.importorskip("moto", reason="moto is required for these tests")

import boto3  # noqa: E402

from src.managers.components.file_attachments import (  # noqa: E402
    MAX_ATTACH_FILES,
    FileAttachments,
)
from src.models.authz import Principal  # noqa: E402
from src.models.errors import CodedError, ErrorCode  # noqa: E402
from src.models.uploaded_file import UploadedFile  # noqa: E402
from src.services.s3_service import S3Service, S3ServiceError  # noqa: E402
from src.services.uploaded_file_store import (  # noqa: E402
    UploadedFileStore,
    UploadedFileStoreError,
)
from tests.conftest import ADMIN, MEMBER_A, MEMBER_B, NO_ROLE  # noqa: E402
from tests.error_helpers import raises_code  # noqa: E402
from tests.log_helpers import LogCapture  # noqa: E402

REGION = "us-east-1"
TABLE = "UploadedFilesU5"
BUCKET = "u5-attach-bucket"
NOW = datetime(2026, 10, 14, 3, 0, 0, tzinfo=timezone.utc)
NOW_EPOCH = int(NOW.timestamp())


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        yield ddb


@pytest.fixture
def store(aws) -> UploadedFileStore:
    return UploadedFileStore(region=REGION, table_name=TABLE, client=aws)


@pytest.fixture
def s3(aws) -> S3Service:
    return S3Service(BUCKET, REGION)


class Clock:
    def __init__(self, now: int = NOW_EPOCH) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def files(store, s3, clock) -> FileAttachments:
    return FileAttachments(store, s3, clock=clock)


@pytest.fixture
def logs(capsys) -> LogCapture:
    return LogCapture(capsys)


def _upload(
    store: UploadedFileStore, s3: S3Service, owner: str = MEMBER_A.sub
) -> UploadedFile:
    uploaded = UploadedFile.create_new(
        owner_sub=owner,
        file_type="discussion_document",
        original_filename="資料.pdf",
        file_size=3,
        file_hash="h",
        mime_type="application/pdf",
        now=NOW,
    )
    path = s3.upload_file(b"pdf", uploaded.s3_key, tags={"pending": "true"})
    uploaded = uploaded.with_path(path)
    store.put(uploaded)
    return uploaded


def _tags(s3: S3Service, f: UploadedFile) -> dict[str, str]:
    return s3.get_object_tags(f.s3_key)


A = MEMBER_A.principal()
B = MEMBER_B.principal()


# =============================================================================
# attach_all（手順 1〜6）
# =============================================================================


class TestAttach:
    def test_attaches_and_clears_the_pending_tag(self, files, store, s3):
        a, b = _upload(store, s3), _upload(store, s3)

        result = files.attach_all(A, [a.id, b.id, a.id], "discussion", "d1")

        assert set(result.file_ids) == {a.id, b.id}
        assert result.original_expires_at[a.id] == a.expires_at
        for f in (a, b):
            stored = store.get(f.id)
            assert (stored.parent_type, stored.parent_id) == ("discussion", "d1")
            assert stored.expires_at is None
            assert _tags(s3, f) == {}

    def test_no_files_does_nothing(self, files):
        result = files.attach_all(A, [], "discussion", "d1")
        assert result.file_ids == ()

    def test_more_than_100_files_changes_nothing(self, files, store, s3):
        assert MAX_ATTACH_FILES == 100
        a = _upload(store, s3)
        ids = [a.id] + [f"00000000-0000-4000-8000-{i:012d}" for i in range(100)]
        with raises_code(
            CodedError, ErrorCode.FILE_ATTACH_LIMIT_EXCEEDED, max_files=100
        ):
            files.attach_all(A, ids, "discussion", "d1")
        assert _tags(s3, a) == {"pending": "true"}
        assert store.get(a.id).parent_id is None

    @pytest.mark.parametrize(
        "case", ["other", "expired", "attached", "missing", "bad_id"]
    )
    def test_any_unusable_file_is_not_found_and_changes_nothing(
        self, files, store, s3, clock, case
    ):
        good = _upload(store, s3)
        if case == "other":
            bad_id = _upload(store, s3, owner=MEMBER_B.sub).id
        elif case == "expired":
            bad = _upload(store, s3)
            bad_id = bad.id
            clock.now = bad.expires_at or 0  # exactly at the expiry
        elif case == "attached":
            bad = _upload(store, s3)
            files.attach_all(A, [bad.id], "discussion", "d0")
            bad_id = bad.id
        elif case == "missing":
            bad_id = "00000000-0000-4000-8000-000000000000"
        else:
            bad_id = "../../etc/passwd"

        with raises_code(CodedError, ErrorCode.FILE_NOT_FOUND):
            files.attach_all(A, [good.id, bad_id], "discussion", "d1")
        assert store.get(good.id).parent_id is None
        assert _tags(s3, good) == {"pending": "true"}

    def test_a_user_without_roles_attaches_nothing(self, files, store, s3):
        f = _upload(store, s3, owner=NO_ROLE.sub)
        with raises_code(CodedError, ErrorCode.FILE_NOT_FOUND):
            files.attach_all(NO_ROLE.principal(), [f.id], "discussion", "d1")

    def test_without_s3_only_the_metadata_changes(self, store, clock):
        f = UploadedFile.create_new(
            owner_sub=A.sub,
            file_type="survey_image",
            original_filename="a.png",
            file_size=1,
            file_hash="h",
            mime_type="image/png",
            now=NOW,
        ).with_path("/tmp/local/a.png")
        store.put(f)
        attachments = FileAttachments(store, None, clock=clock)

        attachments.attach_all(A, [f.id], "survey_template", "t1")

        assert store.get(f.id).parent_id == "t1"


# =============================================================================
# 失敗と付け直し（手順 3・4・5・7）
# =============================================================================


class FlakyTags:
    """S3Service wrapper: fail chosen tag calls, or run a hook after them."""

    def __init__(self, inner: S3Service) -> None:
        self.inner = inner
        self.fail_delete_on: set[int] = set()
        self.fail_put = False
        self.after_delete: dict[int, Callable[[], None]] = {}
        self.deletes = 0

    def delete_object_tagging(self, key: str) -> None:
        self.deletes += 1
        n = self.deletes
        if n in self.fail_delete_on:
            raise S3ServiceError("boom", code=ErrorCode.S3_OPERATION_FAILED)
        self.inner.delete_object_tagging(key)
        hook = self.after_delete.pop(n, None)
        if hook:
            hook()

    def put_object_tags(self, key: str, tags: dict[str, str]) -> None:
        if self.fail_put:
            raise S3ServiceError("boom", code=ErrorCode.S3_OPERATION_FAILED)
        self.inner.put_object_tags(key, tags)


class HookedStore:
    """UploadedFileStore wrapper: run hooks around the attach transaction."""

    def __init__(self, inner: UploadedFileStore) -> None:
        self.inner = inner
        self.fail_attach = False
        self.fail_detach = False
        self.after_attach: Callable[[], None] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def transact_attach(self, *args: Any, **kwargs: Any) -> None:
        if self.fail_attach:
            raise UploadedFileStoreError("conflict")
        self.inner.transact_attach(*args, **kwargs)
        if self.after_attach:
            hook, self.after_attach = self.after_attach, None
            hook()

    def transact_detach(self, *args: Any, **kwargs: Any) -> None:
        if self.fail_detach:
            raise UploadedFileStoreError("down")
        self.inner.transact_detach(*args, **kwargs)


class TestAttachFailures:
    def test_tag_removal_failure_restores_tags_and_fails(self, store, s3, clock):
        a, b = _upload(store, s3), _upload(store, s3)
        tags = FlakyTags(s3)
        tags.fail_delete_on = {2}  # the second file's tag removal fails
        attachments = FileAttachments(store, tags, clock=clock)

        with raises_code(CodedError, ErrorCode.FILE_OPERATION_FAILED):
            attachments.attach_all(A, [a.id, b.id], "discussion", "d1")
        assert _tags(s3, a) == {"pending": "true"}
        assert _tags(s3, b) == {"pending": "true"}
        assert store.get(a.id).parent_id is None

    def test_transaction_failure_restores_tags_and_fails(self, store, s3, clock):
        a = _upload(store, s3)
        hooked = HookedStore(store)
        hooked.fail_attach = True
        attachments = FileAttachments(hooked, s3, clock=clock)

        with raises_code(CodedError, ErrorCode.FILE_OPERATION_FAILED):
            attachments.attach_all(A, [a.id], "discussion", "d1")
        assert _tags(s3, a) == {"pending": "true"}
        assert store.get(a.id).parent_id is None

    def test_restore_failure_is_logged(self, store, s3, clock, logs):
        a = _upload(store, s3)
        hooked = HookedStore(store)
        hooked.fail_attach = True
        tags = FlakyTags(s3)
        tags.fail_put = True
        attachments = FileAttachments(hooked, tags, clock=clock)

        with raises_code(CodedError, ErrorCode.FILE_OPERATION_FAILED):
            attachments.attach_all(A, [a.id], "discussion", "d1")
        events = logs.events("file.attach_rollback_failed")
        assert [e["step"] for e in events] == ["restore_tag"]
        assert events[0]["file_id"] == a.id
        assert events[0]["parent_type"] == "discussion"
        assert events[0]["error_type"] == "S3ServiceError"
        assert "boom" not in str(events[0])

    def test_failed_second_clear_still_attaches(self, store, s3, clock, logs):
        a = _upload(store, s3)
        tags = FlakyTags(s3)
        tags.fail_delete_on = {2}  # step 5 (the clear after the transaction)
        attachments = FileAttachments(store, tags, clock=clock)

        result = attachments.attach_all(A, [a.id], "discussion", "d1")

        assert result.file_ids == (a.id,)
        assert store.get(a.id).parent_id == "d1"
        assert [e["step"] for e in logs.events("file.attach_rollback_failed")] == [
            "retag_clear"
        ]


class TestConcurrentOrders:
    """security-design.md §3 の並行の順序（A が紐付け、B が同じファイルで付け直し）。"""

    def test_b_restores_between_a_tag_removal_and_a_transaction(self, store, s3, clock):
        f = _upload(store, s3)
        tags = FlakyTags(s3)
        b_side = FileAttachments(store, s3, clock=clock)
        # after A's step 3, B (whose attach failed) runs its step 7
        tags.after_delete[1] = lambda: b_side._restore_tags([f.id], "discussion")
        a_side = FileAttachments(store, tags, clock=clock)

        a_side.attach_all(A, [f.id], "discussion", "dA")

        assert _tags(s3, f) == {}
        assert store.get(f.id).parent_id == "dA"

    def test_b_restores_after_a_transaction_before_a_clear(self, store, s3, clock):
        f = _upload(store, s3)
        hooked = HookedStore(store)
        b_side = FileAttachments(store, s3, clock=clock)
        hooked.after_attach = lambda: b_side._restore_tags([f.id], "discussion")
        a_side = FileAttachments(hooked, s3, clock=clock)

        a_side.attach_all(A, [f.id], "discussion", "dA")

        assert _tags(s3, f) == {}
        assert store.get(f.id).parent_id == "dA"

    def test_restore_takes_its_tag_back_when_the_file_was_attached_meanwhile(
        self, store, s3, clock
    ):
        f = _upload(store, s3)
        s3.delete_object_tagging(f.s3_key)
        tags = FlakyTags(s3)
        other = FileAttachments(store, s3, clock=clock)
        restorer = FileAttachments(store, tags, clock=clock)

        original_put = tags.put_object_tags

        def put_then_attach(key: str, values: dict[str, str]) -> None:
            # between B's read (unattached) and B's re-read, A attaches
            other_store = store
            other_store.transact_attach(A.sub, [f.id], "discussion", "dA", NOW_EPOCH)
            original_put(key, values)

        tags.put_object_tags = put_then_attach  # type: ignore[method-assign]
        restorer._restore_tags([f.id], "discussion")
        assert other is not None
        assert _tags(s3, f) == {}
        assert store.get(f.id).parent_id == "dA"


# =============================================================================
# detach
# =============================================================================


class TestDetach:
    def test_detach_restores_expiry_and_tag(self, files, store, s3):
        f = _upload(store, s3)
        result = files.attach_all(A, [f.id], "discussion", "d1")

        files.detach(result)

        stored = store.get(f.id)
        assert stored.parent_id is None
        assert stored.expires_at == f.expires_at
        assert _tags(s3, f) == {"pending": "true"}

    def test_detach_failure_is_logged_not_raised(self, store, s3, clock, logs):
        f = _upload(store, s3)
        hooked = HookedStore(store)
        attachments = FileAttachments(hooked, s3, clock=clock)
        result = attachments.attach_all(A, [f.id], "discussion", "d1")
        hooked.fail_detach = True

        attachments.detach(result)  # no exception

        assert [e["step"] for e in logs.events("file.attach_rollback_failed")] == [
            "detach"
        ]
        assert store.get(f.id).parent_id == "d1"

    def test_detach_of_nothing_does_nothing(self, files):
        from src.models.uploaded_file import AttachResult

        files.detach(AttachResult.empty("discussion", "d1"))


# =============================================================================
# can_read_file と register_parent（BR5.4、BR5.5、BR5.7）
# =============================================================================


class TestCanReadFile:
    def test_before_attach_only_the_owner_before_expiry(self, files, store, s3, clock):
        f = _upload(store, s3)
        assert files.can_read_file(A, f.id)
        assert not files.can_read_file(B, f.id)
        assert not files.can_read_file(ADMIN.principal(), f.id)
        clock.now = (f.expires_at or 0) - 1
        assert files.can_read_file(A, f.id)
        clock.now = f.expires_at or 0  # exactly at the expiry
        assert not files.can_read_file(A, f.id)

    def test_missing_or_malformed_ids_are_not_readable(self, files):
        assert not files.can_read_file(A, "00000000-0000-4000-8000-000000000000")
        assert not files.can_read_file(A, "not-a-uuid")
        with raises_code(CodedError, ErrorCode.FILE_NOT_FOUND):
            files.readable_file(A, "not-a-uuid")

    def test_attached_without_a_registered_parent_owner_and_admin(
        self, files, store, s3
    ):
        f = _upload(store, s3)
        files.attach_all(A, [f.id], "discussion", "d1")

        assert files.can_read_file(A, f.id)
        assert files.can_read_file(ADMIN.principal(), f.id)
        # a department member is not served until U6 registers discussions
        sales_peer = Principal(
            sub="local:peer@example.com",
            roles=A.roles,
            group_ids=A.group_ids,
        )
        assert not files.can_read_file(sales_peer, f.id)
        assert not files.can_read_file(B, f.id)

    def test_registered_parent_decides(self, files, store, s3):
        f = _upload(store, s3)
        files.attach_all(A, [f.id], "discussion", "d1")
        reader = Mock(return_value=True)
        files.register_parent("discussion", reader)

        assert files.can_read_file(B, f.id)
        reader.assert_called_with(B, "d1")
        reader.return_value = False
        assert not files.can_read_file(A, f.id)

    def test_a_parent_type_is_registered_once(self, files):
        files.register_parent("survey_template", Mock())
        with pytest.raises(ValueError):
            files.register_parent("survey_template", Mock())
        with pytest.raises(ValueError):
            files.register_parent("interview", Mock())

    def test_readable_file_returns_the_metadata(self, files, store, s3):
        f = _upload(store, s3)
        assert files.readable_file(A, f.id) == f
        with raises_code(CodedError, ErrorCode.FILE_NOT_FOUND):
            files.readable_file(B, f.id)

    def test_denials_are_logged(self, files, store, s3, logs):
        f = _upload(store, s3)
        assert not files.can_read_file(B, f.id)
        denied = logs.events("authz.denied")
        assert denied and denied[-1]["sub"] == B.sub
        assert f.id not in str(denied[-1])


# =============================================================================
# 組み立て（logical-components.md「組み立て」、約束 C3）
# =============================================================================


class TestAssembly:
    def test_one_instance_per_task_with_the_three_lookups(self):
        from src.managers.components.sharing_runtime import (
            build_access_parts,
            reset_shared_parts,
            shared_access_parts,
        )
        from src.models.authz import ResourceType

        lookup_service = Mock()
        lookup_service.reader.side_effect = lambda table: table
        reset_shared_parts()
        try:
            parts = shared_access_parts(
                lambda: build_access_parts(lookup_service, Mock(), None)
            )
            assert shared_access_parts(lambda: pytest.fail("built twice")) is parts
            registered = {
                call.args[0].resource_type: call.args[0].display_name_attribute
                for call in lookup_service.reader.call_args_list
            }
            assert registered == {
                ResourceType.PERSONA: "name",
                ResourceType.DISCUSSION: "topic",
                ResourceType.DATASET: "name",
                ResourceType.KNOWLEDGE_BASE: "name",
            }
            assert isinstance(parts.files, FileAttachments)
        finally:
            reset_shared_parts()

    def test_discussion_parent_is_registered_with_the_discussion_readers(
        self, store, s3, clock
    ):
        """U6 BR5.1: 議論を親とする資料は、議論を読める人に配信する（NFR1.53）。"""
        from src.managers.components.sharing_runtime import build_access_parts
        from src.models.authz import AccessTarget, ResourceType

        def reader(table: Any) -> Any:
            class _Reader:
                def read(self, ids: Any, deadline: float) -> dict:
                    if table.resource_type is not ResourceType.DISCUSSION:
                        return {}
                    return {
                        i: AccessTarget(
                            ResourceType.DISCUSSION,
                            i,
                            owner_sub=A.sub,
                            shared_group_ids=frozenset({"sales"}),
                        )
                        for i in ids
                        if i == "d1"
                    }

            return _Reader()

        lookup_service = Mock()
        lookup_service.reader.side_effect = reader
        parts = build_access_parts(lookup_service, store, s3, clock=clock)
        f = _upload(store, s3)
        g = _upload(store, s3)
        parts.files.attach_all(A, [f.id], "discussion", "d1")
        parts.files.attach_all(A, [g.id], "discussion", "gone")
        sales_peer = Principal(sub="peer", roles=A.roles, group_ids=A.group_ids)

        assert parts.files.can_read_file(A, f.id)
        assert parts.files.can_read_file(sales_peer, f.id)
        assert not parts.files.can_read_file(B, f.id)
        with raises_code(CodedError, ErrorCode.FILE_NOT_FOUND):
            parts.files.readable_file(B, f.id)
        assert not parts.files.can_read_file(A, g.id)


class TestDeleteAttached:
    """U6 BR1.5 (3): 議論の削除で、紐付いた資料（項目と S3 のオブジェクト）を消す。"""

    def test_deletes_the_metadata_and_the_object(self, files, store, s3):
        f = _upload(store, s3)
        files.attach_all(A, [f.id], "discussion", "d1")
        assert files.delete_attached("discussion", "d1", [f.id]) == []
        assert store.get(f.id) is None
        with pytest.raises(S3ServiceError):
            s3.download_file(f.file_path)

    def test_files_of_another_parent_are_left_alone(self, files, store, s3):
        f = _upload(store, s3)
        files.attach_all(A, [f.id], "discussion", "other")
        unattached = _upload(store, s3)
        assert files.delete_attached("discussion", "d1", [f.id, unattached.id]) == []
        assert store.get(f.id) is not None
        assert store.get(unattached.id) is not None

    def test_failures_are_returned(self, store, s3, clock):
        failing = Mock(wraps=s3)
        failing.delete_file.side_effect = RuntimeError("s3 down")
        files = FileAttachments(store, failing, clock=clock)
        f = _upload(store, s3)
        files.attach_all(A, [f.id], "discussion", "d1")
        assert files.delete_attached("discussion", "d1", [f.id]) == [f.id]
        assert store.get(f.id) is not None

    def test_nothing_to_delete(self, files):
        assert files.delete_attached("discussion", "d1", []) == []

    def test_reset_forgets_the_parts(self):
        from src.managers.components.sharing_runtime import (
            build_access_parts,
            reset_shared_parts,
            shared_access_parts,
        )

        lookup_service = Mock()
        first = shared_access_parts(
            lambda: build_access_parts(lookup_service, Mock(), None)
        )
        reset_shared_parts()
        second = shared_access_parts(
            lambda: build_access_parts(lookup_service, Mock(), None)
        )
        reset_shared_parts()
        assert first is not second
