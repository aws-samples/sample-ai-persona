"""
Integration test for multimodal document support (Task 1-4).
Tests the complete flow from file upload to discussion with documents.
"""

from pathlib import Path
from unittest.mock import Mock

from src.managers.file_manager import FileManager
from src.managers.discussion_manager import DiscussionManager
from src.models.persona import Persona
from src.models.message import Message


def test_complete_document_flow(tmp_path):
    """Test complete flow: upload documents -> start discussion with documents."""

    # Setup with mock services
    mock_db = Mock()
    mock_db.save_persona.return_value = "persona-id"
    file_manager = FileManager(db_service=mock_db)
    file_manager.discussion_doc_dir = tmp_path / "discussion_documents"
    file_manager.discussion_doc_dir.mkdir()

    # Test files
    test_image_path = Path(__file__).parent.parent / "test_file" / "test_image.jpeg"
    test_pdf_path = Path(__file__).parent.parent / "test_file" / "pdf_test.pdf"

    assert test_image_path.exists(), f"Test image not found: {test_image_path}"
    assert test_pdf_path.exists(), f"Test PDF not found: {test_pdf_path}"

    print("✓ Test files found")
    print(f"  - Image: {test_image_path}")
    print(f"  - PDF: {test_pdf_path}")

    # Step 1: Upload documents
    print("\n[Step 1] Uploading documents...")

    with open(test_image_path, "rb") as f:
        image_content = f.read()
    image_metadata = file_manager.upload_discussion_document(
        image_content, "test_image.jpeg"
    )
    print(f"✓ Image uploaded: {image_metadata.file_id}")

    with open(test_pdf_path, "rb") as f:
        pdf_content = f.read()
    pdf_metadata = file_manager.upload_discussion_document(pdf_content, "pdf_test.pdf")
    print(f"✓ PDF uploaded: {pdf_metadata.file_id}")

    # Setup mock for get_uploaded_file_info
    file_info_map = {
        image_metadata.file_id: {
            "file_path": str(file_manager.discussion_doc_dir / image_metadata.saved_filename),
            "mime_type": "image/jpeg",
            "file_size": len(image_content),
            "filename": "test_image.jpeg",
            "original_filename": "test_image.jpeg",
            "uploaded_at": image_metadata.uploaded_at.isoformat() if hasattr(image_metadata, 'uploaded_at') else "2026-01-01T00:00:00",
        },
        pdf_metadata.file_id: {
            "file_path": str(file_manager.discussion_doc_dir / pdf_metadata.saved_filename),
            "mime_type": "application/pdf",
            "file_size": len(pdf_content),
            "filename": "pdf_test.pdf",
            "original_filename": "pdf_test.pdf",
            "uploaded_at": pdf_metadata.uploaded_at.isoformat() if hasattr(pdf_metadata, 'uploaded_at') else "2026-01-01T00:00:00",
        },
    }
    mock_db.get_uploaded_file_info.side_effect = lambda doc_id: file_info_map.get(doc_id)

    # Step 2: Create test personas
    print("\n[Step 2] Creating test personas...")

    persona1 = Persona.create_new(
        name="田中花子",
        age=30,
        occupation="マーケティング担当",
        background="東京在住のマーケティング担当者。データ分析が得意。",
        values=["効率性", "革新性", "データドリブン"],
        pain_points=["時間不足", "情報過多", "リソース制約"],
        goals=["キャリアアップ", "スキル向上", "チーム成長"],
    )

    persona2 = Persona.create_new(
        name="佐藤太郎",
        age=35,
        occupation="商品開発者",
        background="大阪在住の商品開発者。ユーザー視点を重視。",
        values=["品質", "顧客満足", "使いやすさ"],
        pain_points=["予算制約", "技術的課題", "市場競争"],
        goals=["新商品開発", "市場拡大", "ブランド構築"],
    )

    # Save personas
    mock_db.save_persona(persona1)
    mock_db.save_persona(persona2)
    print(f"✓ Personas created: {persona1.name}, {persona2.name}")

    # Step 3: Start discussion with documents
    print("\n[Step 3] Starting discussion with documents...")
    print(f"  Document IDs: [{image_metadata.file_id}, {pdf_metadata.file_id}]")

    # Mock AI service for testing (to avoid actual API calls)
    mock_ai_service = Mock()
    mock_ai_service.facilitate_discussion.return_value = [
        Message.create_new(
            persona1.id,
            persona1.name,
            "この画像とPDFドキュメントを見ると、顧客のニーズが明確に表れています。特に効率性を重視する傾向が強く、データに基づいた意思決定を求めていることがわかります。",
        ),
        Message.create_new(
            persona2.id,
            persona2.name,
            "そうですね。ドキュメントから読み取れる情報を基に、品質と使いやすさを両立させた商品開発が必要です。顧客満足度を高めるためには、これらの要素を統合的に考える必要があります。",
        ),
    ]

    discussion_manager_with_mock = DiscussionManager(
        ai_service=mock_ai_service, database_service=mock_db
    )

    discussion = discussion_manager_with_mock.start_discussion(
        personas=[persona1, persona2],
        topic="新商品のマーケティング戦略について、提供されたドキュメントを参考に議論してください",
        document_ids=[image_metadata.file_id, pdf_metadata.file_id],
    )

    print(f"✓ Discussion created: {discussion.id}")
    print(f"  - Messages: {len(discussion.messages)}")
    print(f"  - Documents: {len(discussion.documents)}")

    # Step 4: Verify results
    print("\n[Step 4] Verifying results...")

    assert discussion is not None, "Discussion should be created"
    assert len(discussion.messages) == 2, "Should have 2 messages"
    assert discussion.documents is not None, "Should have documents"
    assert len(discussion.documents) == 2, "Should have 2 documents"

    # Verify document metadata
    doc_filenames = [doc["filename"] for doc in discussion.documents]
    assert "test_image.jpeg" in doc_filenames, "Image should be in documents"
    assert "pdf_test.pdf" in doc_filenames, "PDF should be in documents"

    print("✓ All verifications passed")

    # Verify AI service was called with documents
    call_args = mock_ai_service.facilitate_discussion.call_args
    assert call_args is not None, "AI service should be called"
    assert "documents" in call_args[1], "Should pass documents parameter"
    assert call_args[1]["documents"] is not None, "Documents should not be None"
    assert len(call_args[1]["documents"]) == 2, "Should pass 2 documents"

    print("✓ AI service called with correct documents")

    # Step 5: Save and retrieve discussion
    print("\n[Step 5] Saving and retrieving discussion...")

    mock_db.save_discussion.return_value = discussion.id
    mock_db.get_discussion.return_value = discussion

    discussion_id = mock_db.save_discussion(discussion)
    print(f"✓ Discussion saved: {discussion_id}")

    retrieved_discussion = mock_db.get_discussion(discussion_id)
    assert retrieved_discussion is not None, "Should retrieve discussion"
    assert retrieved_discussion.documents is not None, (
        "Retrieved discussion should have documents"
    )
    assert len(retrieved_discussion.documents) == 2, (
        "Retrieved discussion should have 2 documents"
    )

    print("✓ Discussion retrieved with documents intact")

    # Print summary
    print("\n" + "=" * 60)
    print("✅ INTEGRATION TEST PASSED")
    print("=" * 60)
    print(f"Discussion ID: {discussion.id}")
    print(f"Topic: {discussion.topic}")
    print(f"Participants: {len(discussion.participants)}")
    print(f"Messages: {len(discussion.messages)}")
    print(f"Documents: {len(discussion.documents)}")
    print("\nDocument Details:")
    for i, doc in enumerate(discussion.documents, 1):
        print(
            f"  {i}. {doc['filename']} ({doc['mime_type']}, {doc['file_size']} bytes)"
        )
    print("=" * 60)


if __name__ == "__main__":
    test_complete_document_flow()
