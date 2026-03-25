---
inclusion: always
---

# Technical Guidelines & Architecture Standards

## Core Technology Stack

- **Python 3.13** - Primary development language
- **uv** - Package manager and dependency management
- **FastAPI + htmx** - Frontend framework
- **Jinja2** - Template engine
- **Tailwind CSS** - CSS framework
- **Alpine.js** - Lightweight JavaScript framework
- **Amazon DynamoDB** - NoSQL database for data persistence
- **Amazon Bedrock (Claude Sonnet 4.5 / Claude Haiku 4.5)** - AI service (Batch Inference support)
- **Strands Agent SDK** - AI Agent Framework
- **Amazon Bedrock AgentCore Memory** - Persona long-term memory (optional)
- **DuckDB** - Fast SQL queries on S3 Parquet files (mass survey)
- **Polars** - High-performance DataFrame library (memory-efficient data processing)
- **boto3** - AWS SDK for Python
- **markitdown** - File to markdown conversion library

## Code Style & Standards

### Python Conventions

- Follow PEP 8 style guidelines
- Use type hints for all function parameters and return values
- Prefer standard library solutions over external dependencies
- Use dataclasses for model definitions
- Implement proper error handling with custom exceptions

### Project Structure Patterns

- **Manager Pattern**: Business logic in `src/managers/` (PersonaManager, DiscussionManager, AgentDiscussionManager, InterviewManager, FileManager, SurveyManager, DatasetManager)
- **Service Layer**: External integrations in `src/services/` (ai_service, agent_service, database_service, s3_service, survey_service, memory/, knowledge_base/)
- **Model Classes**: Data structures in `src/models/` (Persona, Discussion, Message, Insight, InsightCategory, SurveyTemplate, Survey, InsightReport, VisualAnalysisData, Memory, Dataset, KnowledgeBase, PersonaKBBinding)

### Database Guidelines

#### DynamoDB

The system uses Amazon DynamoDB as its database backend:
- **Production**: AWS DynamoDB in the cloud

Configuration via environment variables:
```bash
DYNAMODB_TABLE_PREFIX=AIPersona
DYNAMODB_REGION=us-east-1
```

#### DynamoDB Best Practices

- Configure AWS credentials via environment variables or IAM roles
- Use DatabaseService which provides a unified interface
- Implement retry logic with exponential backoff for throttling errors
- Use Global Secondary Indexes (GSIs) for query operations
- Handle DynamoDB-specific errors (throttling, network issues, conditional checks)
- Use batch operations for bulk writes during migrations
- Monitor DynamoDB usage and costs in AWS Console

#### Service Factory Pattern

- Always use `ServiceFactory.get_database_service()` to instantiate database services
- The factory returns a configured DatabaseService instance
- Managers should never directly instantiate DatabaseService
- This pattern enables consistent configuration across the application

### File Management

- Local: `uploads/` directory with UUID-based naming (when `S3_BUCKET_NAME` is not set)
- AWS: Stored in S3 bucket (auto-created during CDK deployment)
- Use FileManager for all file operations
- Discussion documents: `discussion_documents/`
- Survey images: `survey_images/`
- Knowledge files: `knowledge_files/`
- Implement proper file validation and error handling

### AI Service Integration

- All AI interactions go through `src/services/ai_service.py` or `src/services/agent_service.py`
- Bedrock Converse API for multimodal support (images, PDFs)
- Implement proper retry logic and error handling for AI service calls
- Use structured prompts for consistent AI responses
- Handle rate limiting and API errors gracefully
- Batch Inference via `src/services/survey_service.py` for mass surveys

### Testing Standards

- Unit tests in `tests/unit/` for individual components
- Integration tests in `tests/integration/` for workflow testing
- API tests in `tests/api/` for endpoint testing
- Use pytest as the testing framework
- Mock external dependencies (AI service, file system) in unit tests
- Test both success and error scenarios

### htmx UI Guidelines

- Use FastAPI routers in `web/routers/` for endpoint definitions
- Use Jinja2 templates in `web/templates/` for HTML rendering
- Use htmx attributes for dynamic content updates without full page reloads
- Use partials in `web/templates/*/partials/` for htmx swap targets
- Use Alpine.js for lightweight client-side interactivity
- Use Tailwind CSS for styling
- Implement proper error handling with error partials
- Use hx-indicator for loading states
- Follow RESTful URL patterns for htmx endpoints
- SSE endpoints for real-time streaming (discussions, interviews)

### Running the Application

```bash
uv run python run_htmx.py
# or
uv run uvicorn web.main:app --reload --port 8000
```
