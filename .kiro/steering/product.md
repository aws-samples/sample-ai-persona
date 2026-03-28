---
inclusion: always
---

# AI Persona System - Product Guidelines

## Product Overview

**AI Persona** is a system that generates AI personas from interview data and research reports, facilitates discussions between personas, and enables direct interviews to generate insights for product planning and marketing strategy.

## Target Users

- Marketing planners and strategists
- Product development teams
- Business analysts working on customer insights

## Core Features

### 1. Persona Generation

#### Generating from Interview Data
- Upload N1 interview text files (loyal customer interviews)
- Supported formats: `.txt`, `.md`
- Generate AI personas using Amazon Bedrock Claude Sonnet 4.5
- Display generated personas with edit and save capabilities
- Store personas in DynamoDB database for reuse

#### Generating Multiple Personas from Research Reports
- Upload market research reports
- Supported formats: `.pdf`, `.docx`, `.doc`, `.txt`, `.md`
- Generate 1-10 personas at once from a single report
- Strands Agent SDK-based analysis agent parses reports
- Select which generated persona candidates to save

### 2. Persona Discussion & Insight Generation

- Select multiple personas for discussion participation
- Three discussion modes: Classic (fast), Agent (deep), Interview (real-time)
- Dynamic confidence scores (0-100%) for insights
- Custom insight categories (1-10 categories per discussion)
- Multimodal document support (images, PDFs) in all modes
- Supported discussion document formats: `.png`, `.jpg`, `.jpeg`, `.pdf`
- Save discussion results and insights for future reference

### 3. Interview Mode (Real-time Persona Interaction)

- Direct chat-based interaction with AI personas
- Real-time question and answer sessions
- Support for 1-5 personas in a single interview session
- Chat UI with visual persona distinction and timestamps
- Session saving and history management
- Integration with existing discussion results system
- Long-term memory support for personas to remember and utilize past discussions (optional)
- Multimodal document support

### 4. Persona Long-term Memory (AgentCore Memory)

- Two memory strategies: Summary (discussion memory) and Semantic (knowledge/facts)
- Per-persona memory isolation
- Knowledge addition via manual input or file upload
- Supported knowledge file formats: `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.txt`, `.md`
- markitdown-based automatic markdown conversion
- Memory browsing and deletion in persona detail view

### 5. Mass Survey

- Large-scale AI persona surveys (hundreds to thousands of respondents)
- **Persona data setup**: Nemotron-Personas-Japan (open dataset) download or custom CSV upload
- **Column mapping**: Map arbitrary CSV columns to standard persona attributes (13 items) with auto-mapping
- **Datasource selection**: Choose between Nemotron or custom datasets at survey start
- Bedrock Batch Inference for async large-scale inference
- Survey templates: single/multi choice, free text, scale questions, image attachments
- Attribute filtering: dynamically updated based on selected datasource
- Result analysis: CSV download, visual charts, AI insight reports
- DuckDB + Polars for memory-efficient data processing (thread-safe per-datasource connections)

### 6. External Dataset Integration (Experimental)

- CSV upload with automatic schema detection
- MotherDuck MCP Server for SQL query execution
- Persona-dataset linkage for data-driven discussions

## Architecture Patterns

- **FastAPI + htmx application** with page-based navigation
- **Manager pattern** for business logic (PersonaManager, DiscussionManager, AgentDiscussionManager, InterviewManager, FileManager, SurveyManager, DatasetManager)
- **Service layer** for external integrations (AI, DB, S3, Memory, Survey, KnowledgeBase)
- **Model classes** for data structures (Persona, Discussion, Message, Insight, InsightCategory, SurveyTemplate, TemplateImage, Survey, InsightReport, VisualAnalysisData, PersonaStatistics, MemoryEntry, Dataset, DatasetColumn, PersonaDatasetBinding, KnowledgeBase, PersonaKBBinding)
- **Component-based UI** with Jinja2 templates, htmx, Alpine.js, Tailwind CSS

## Data Flow

1. File upload → FileManager → AI Service → Persona generation
2. Report upload → AgentService → Multiple persona generation
3. Persona selection → DiscussionManager → AI Service → Discussion execution
4. Discussion results → Insight generation (with custom categories) → Database storage
5. Interview mode → InterviewManager → AgentService → Real-time persona responses → Session storage
6. Survey template → Persona selection → Batch Inference → Results analysis

## Key Business Rules

- Personas generated from uploaded interview files or research reports
- Classic discussions require minimum 2 personas, maximum recommended 5-6
- Interview mode supports 1-5 personas for direct interaction
- Each discussion/interview generates both conversation logs and structured insights
- All data persists in DynamoDB for cloud-based storage
- File uploads stored locally (`uploads/`) or in S3 (when `S3_BUCKET_NAME` is set)
- Interview sessions maintain real-time state until explicitly saved
- Multimodal documents: individual 10MB limit, 32MB total (Bedrock API constraint)

## User Experience Principles

- Simple, wizard-like workflow for complex operations
- Clear progress indicators during AI processing
- Comprehensive error handling with user-friendly messages
- Ability to review and edit AI-generated content before saving
- Persistent storage of all work for session continuity
- Real-time streaming display for discussions and interviews via SSE
