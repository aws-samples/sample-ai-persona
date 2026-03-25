---
inclusion: always
---

# Architecture Patterns & System Design

## Layered Architecture (Strict)

```
Presentation → Application → Service → Agent → Data
```

**Critical Rule**: No layer skipping. UI never calls DatabaseService directly.

| Layer | Components | Responsibility |
|-------|------------|----------------|
| Presentation | `web/` | User interaction, input validation |
| Application | `src/managers/` | Business logic, workflow orchestration |
| Service | `src/services/` | External integrations (AI, DB, S3, Memory) |
| Agent | Persona/Facilitator Agents | Multi-agent discussions via Strands SDK |
| Data | DynamoDB, S3, `uploads/` | Persistence |

## Component Rules

### Managers (Application Layer)
- All business logic lives here
- Coordinate between services
- Return domain models: `Persona`, `Discussion`, `Message`, `Insight`, `SurveyTemplate`, `Survey`
- Never instantiate database services directly—use `ServiceFactory`
- `InterviewManager`: Manages real-time chat sessions with personas
- `AgentDiscussionManager`: Manages agent-driven deep discussions
- `SurveyManager`: Manages mass survey workflows (template, job, results)
- `DatasetManager`: Manages external dataset CRUD and persona linkage

### Services (Service Layer)
- `AIService`: Amazon Bedrock direct calls (Converse API, multimodal support)
- `AgentService`: Strands Agent SDK integration
- `DatabaseService`/`DynamoDBService`: Data persistence
- `S3Service`: File storage (uploads, survey data, discussion documents)
- `SurveyService`: Batch Inference job management
- `MemoryService`: AgentCore Memory integration (summary + semantic strategies)
- `MCPServerManager`: MCP server lifecycle for external dataset SQL queries
- No business logic—external integration only

### Agents
- Created/managed by `AgentService`
- Always dispose after use (prevent memory leaks)
- System prompts define behavior
- Memory tools available for long-term memory enabled sessions

## Discussion Modes

| Mode | Manager | Speed | Use Case | Long-term Memory | Multimodal |
|------|---------|-------|----------|------------------|------------|
| Traditional | `DiscussionManager` | 3-5 min | Speed, simplicity | ❌ | ✅ |
| Agent | `AgentDiscussionManager` | 5-15 min | Depth, natural dialogue | ✅ | ✅ |
| Interview | `InterviewManager` | Real-time | Direct Q&A with personas | ✅ | ✅ |

Check `discussion.mode` field ("traditional", "agent", or "interview") to select manager.

## Data Models (`src/models/`)

All models are dataclasses with type hints:
- `Persona`: demographics, values, pain_points, goals
- `Discussion`: topic, participants, messages, insights, mode, documents
- `Message`: persona_id, content, timestamp, message_type, round_number
- `Insight`: category, description, supporting_messages, confidence_score (dynamic 0-100%)
- `InsightCategory`: custom category with name and description
- `SurveyTemplate`: questions (single/multi choice, free text, scale), images
- `Survey`: batch inference job tracking, results
- `InsightReport`: AI-generated insight report for survey results
- `VisualAnalysisData`: visual analysis data for survey charts
- `Memory`: long-term memory entries (summary, semantic)
- `Dataset`: external dataset metadata and persona linkage
- `KnowledgeBase`: knowledge base metadata for persona knowledge
- `PersonaKBBinding`: persona-knowledge base linkage

Models are immutable—use `replace()` for updates.

## Database

### DynamoDB Backend
```bash
DYNAMODB_TABLE_PREFIX=AIPersona
DYNAMODB_REGION=us-east-1
```

Always use service factory:
```python
from src.services.service_factory import ServiceFactory

service_factory = ServiceFactory()
db_service = service_factory.get_database_service()
```

### DynamoDB Tables
- `{prefix}_Personas`, `{prefix}_Discussions`, `{prefix}_UploadedFiles`
- Survey tables: `{prefix}_SurveyTemplates`, `{prefix}_Surveys`
- Dataset tables: `{prefix}_Datasets`, `{prefix}_PersonaDatasetBindings`
- Knowledge tables: `{prefix}_KnowledgeBases`, `{prefix}_PersonaKBBindings`
- Partition key: `id` (String)
- Dates as ISO 8601 strings
- Implement retry with exponential backoff

## File Storage

### Local Development
```
Upload → Validate (type, size < 10MB) → Save to uploads/{uuid}_{filename} → Store path in DB
```

### AWS Environment (S3)
```
Upload → Validate → Save to S3 bucket → Store S3 key in DB
```

- `S3_BUCKET_NAME` env var controls storage backend (unset = local `uploads/`)
- All file operations through `FileManager` only
- Discussion documents stored in `discussion_documents/`
- Survey images stored in `survey_images/`
- Knowledge files stored in `knowledge_files/`

## Long-term Memory (AgentCore Memory)

- Two strategies: Summary (discussion memory) and Semantic (knowledge/facts)
- Per-persona memory isolation via namespace
- `MemoryService` manages CRUD operations
- `RetrieveOnlySessionManager` for read-only memory access during discussions
- Knowledge can be added manually or via file upload (markitdown conversion)

## Mass Survey Architecture

```
Persona Data Setup → Template → Datasource Selection → Batch Inference Job → Results → Analysis
```

- **Persona data sources**: Nemotron (open dataset) or custom CSV upload with column mapping
- Custom CSV → Polars read → column mapping/rename → Parquet conversion → S3
- DuckDB connections cached per datasource (thread-safe, no shared state)
- DuckDB + Polars for memory-efficient persona data processing
- Bedrock Batch Inference for async large-scale inference
- S3 for input/output JSONL and Parquet files
- Signed URLs for result CSV downloads

## External Dataset Integration (Experimental)

- CSV upload → schema detection → DynamoDB metadata storage
- MotherDuck MCP Server for SQL query execution
- Persona-dataset linkage via configurable key columns

## Error Handling

| Audience | Approach |
|----------|----------|
| Users | Generic, friendly messages |
| Logs | Detailed errors with stack traces |

Never expose: API keys, internal paths, detailed exceptions.

## Key Patterns

### Manager Initialization
```python
config = Config()
service_factory = ServiceFactory()
db_service = service_factory.get_database_service()
manager = SomeManager(database_service=db_service)
```

### htmx Endpoints (FastAPI)
- Routers in `web/routers/` (persona, discussion, interview, survey, settings, api)
- Templates in `web/templates/`
- Partials for htmx swap targets
- SSE endpoints for real-time streaming

## Testing

| Type | Location | Approach |
|------|----------|----------|
| Unit | `tests/unit/` | Mock AI/Agent services |
| Integration | `tests/integration/` | Real temp DB, mock AI |
| API | `tests/api/` | FastAPI TestClient |

Shared fixtures in `tests/conftest.py`:
- `temp_db_path`, `database_service` - DB fixtures
- `sample_persona`, `sample_discussion` - Model fixtures
- `mock_ai_service`, `mock_agent_service` - Mock fixtures
- `client`, `async_client` - FastAPI test clients

Run: `uv run pytest`

## Critical Don'ts

1. Don't skip layers—UI must go through Managers
2. Don't put business logic in services
3. Don't forget to dispose agents
4. Don't expose detailed errors to users
5. Don't instantiate database services directly—use factory
6. Don't skip file validation (type, size)
7. Don't load entire large datasets into memory—use DuckDB/Polars for streaming
