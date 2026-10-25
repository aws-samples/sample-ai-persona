"""
Integration test for multimodal document support (Task 1-4).
Tests the complete flow from file upload to discussion with documents.

U6: the discussion is run by the uploader and saved through
``DiscussionRecords``, which attaches the documents to it.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from src.managers.components.file_attachments import FileAttachments
from src.managers.file_manager import FileManager
from src.services.stores.uploaded_file_store import UploadedFileStore
from tests.conftest import MEMBER_B
from src.managers.discussion_manager import DiscussionManager
from src.models.message import Message
from tests.unit.discussion_world import OWNER, World


@pytest.fixture
def upload_store(monkeypatch):
    """Upload metadata on moto DynamoDB (U5: UploadedFileStore)."""
    moto = pytest.importorskip("moto")
    import boto3

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="UploadedFilesFlow",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield UploadedFileStore(
            region="us-east-1", table_name="UploadedFilesFlow", client=client
        )


def test_complete_document_flow(tmp_path, upload_store):
    """Test complete flow: upload documents -> discussion with documents -> saved."""

    # Setup with mock services (the files themselves are stored locally)
    attachments = FileAttachments(upload_store, None)
    file_manager = FileManager(file_store=upload_store, attachments=attachments)
    file_manager.discussion_doc_dir = tmp_path / "discussion_documents"
    file_manager.discussion_doc_dir.mkdir()
    uploader = OWNER

    test_image_path = Path(__file__).parent.parent / "test_file" / "test_image.jpeg"
    test_pdf_path = Path(__file__).parent.parent / "test_file" / "pdf_test.pdf"
    assert test_image_path.exists(), f"Test image not found: {test_image_path}"
    assert test_pdf_path.exists(), f"Test PDF not found: {test_pdf_path}"

    # Step 1: Upload documents
    image_metadata = file_manager.upload_discussion_document(
        uploader, test_image_path.read_bytes(), "test_image.jpeg"
    )
    pdf_metadata = file_manager.upload_discussion_document(
        uploader, test_pdf_path.read_bytes(), "pdf_test.pdf"
    )

    # U5: the metadata records the uploader; nobody else may read the files
    assert upload_store.get(image_metadata.file_id).owner_sub == uploader.sub
    assert not attachments.can_read_file(MEMBER_B.principal(), pdf_metadata.file_id)

    # Step 2: The personas the uploader may read (owned and shared)
    world = World(files=attachments)
    persona1, persona2 = world.persona_bodies["p1"], world.persona_bodies["p2"]

    # Step 3: Run the discussion with the documents (AI mocked)
    mock_ai_service = Mock()
    mock_ai_service.facilitate_discussion.return_value = [
        Message.create_new(
            persona1.id,
            persona1.name,
            "この画像とPDFドキュメントを見ると、顧客のニーズが明確に表れています。"
            "特に効率性を重視する傾向が強く、データに基づいた意思決定を求めています。",
        ),
        Message.create_new(
            persona2.id,
            persona2.name,
            "ドキュメントから読み取れる情報を基に、品質と使いやすさを両立させた商品開発が"
            "必要です。顧客満足度を高めるには、これらの要素を統合的に考える必要があります。",
        ),
    ]
    mock_ai_service.extract_insights.return_value = []
    manager = DiscussionManager(
        ai_service=mock_ai_service,
        file_attachments=attachments,
        discussion_records=world.records,
        participants=world.participants,
        resource_sharing=world.sharing,
        owners=world.owners,
        s3_service=Mock(),
    )

    discussion = manager.run_classic_discussion(
        uploader,
        [persona1, persona2],
        "新商品のマーケティング戦略について、提供されたドキュメントを参考に議論してください",
        document_ids=[image_metadata.file_id, pdf_metadata.file_id],
    )

    # Step 4: The AI got both documents
    call_args = mock_ai_service.facilitate_discussion.call_args
    assert call_args is not None
    assert len(call_args.kwargs["documents"]) == 2

    # Step 5: The saved discussion keeps the references and owns the files
    view = manager.get_discussion(uploader, discussion.id)
    assert len(view.messages) == 2
    assert sorted(d.filename for d in view.documents) == [
        "pdf_test.pdf",
        "test_image.jpeg",
    ]
    for file_id in (image_metadata.file_id, pdf_metadata.file_id):
        stored = upload_store.get(file_id)
        assert (stored.parent_type, stored.parent_id) == ("discussion", discussion.id)
