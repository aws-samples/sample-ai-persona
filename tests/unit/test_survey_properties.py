"""
Property-based tests for Mass Survey data models.

Feature: mass-survey
"""

from datetime import datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from src.models.survey_template import Question, SurveyTemplate
from src.models.survey import Survey, InsightReport


# =============================================================================
# Strategies
# =============================================================================

valid_question_types = st.sampled_from(["multiple_choice", "free_text", "scale_rating"])

question_strategy = st.builds(
    Question,
    id=st.uuids().map(str),
    text=st.text(min_size=1, max_size=200),
    question_type=valid_question_types,
    options=st.lists(st.text(min_size=1, max_size=50), min_size=0, max_size=10),
    scale_min=st.integers(min_value=1, max_value=5),
    scale_max=st.integers(min_value=1, max_value=10),
)

datetime_strategy = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2099, 12, 31),
)

survey_template_strategy = st.builds(
    SurveyTemplate,
    id=st.uuids().map(str),
    name=st.text(min_size=1, max_size=100),
    questions=st.lists(question_strategy, min_size=0, max_size=10),
    created_at=datetime_strategy,
    updated_at=datetime_strategy,
)


# =============================================================================
# Property 1: SurveyTemplateシリアライゼーションのラウンドトリップ
# Validates: Requirements 11.1
# =============================================================================


@given(template=survey_template_strategy)
@settings(max_examples=100)
def test_survey_template_serialization_roundtrip(template: SurveyTemplate) -> None:
    """
    Property 1: SurveyTemplateシリアライゼーションのラウンドトリップ

    任意の有効なSurveyTemplateオブジェクトに対して、
    from_dict(to_dict(template))は元のオブジェクトと等価なオブジェクトを生成する。

    **Validates: Requirements 11.1**
    """
    restored = SurveyTemplate.from_dict(template.to_dict())

    assert restored.id == template.id
    assert restored.name == template.name
    assert len(restored.questions) == len(template.questions)
    for orig, rest in zip(template.questions, restored.questions):
        assert rest.id == orig.id
        assert rest.text == orig.text
        assert rest.question_type == orig.question_type
        assert rest.options == orig.options
        assert rest.scale_min == orig.scale_min
        assert rest.scale_max == orig.scale_max
    assert restored.created_at == template.created_at
    assert restored.updated_at == template.updated_at


# =============================================================================
# Strategies for Survey
# =============================================================================

insight_report_strategy = st.builds(
    InsightReport,
    id=st.uuids().map(str),
    survey_id=st.uuids().map(str),
    content=st.text(min_size=1, max_size=500),
    created_at=datetime_strategy,
)

survey_strategy = st.builds(
    Survey,
    id=st.uuids().map(str),
    name=st.text(min_size=1, max_size=100),
    description=st.text(min_size=0, max_size=200),
    template_id=st.uuids().map(str),
    persona_count=st.integers(min_value=1, max_value=9999),
    filters=st.one_of(
        st.none(), st.fixed_dictionaries({"gender": st.text(min_size=1, max_size=10)})
    ),
    status=st.sampled_from(["pending", "running", "completed", "error"]),
    s3_result_path=st.one_of(st.none(), st.text(min_size=1, max_size=100)),
    insight_report=st.one_of(st.none(), insight_report_strategy),
    created_at=datetime_strategy,
    updated_at=datetime_strategy,
    error_message=st.one_of(st.none(), st.text(min_size=1, max_size=200)),
)


# =============================================================================
# Property 2: Surveyシリアライゼーションのラウンドトリップ
# Validates: Requirements 11.2
# =============================================================================


@given(survey=survey_strategy)
@settings(max_examples=100)
def test_survey_serialization_roundtrip(survey: Survey) -> None:
    """
    Property 2: Surveyシリアライゼーションのラウンドトリップ

    任意の有効なSurveyオブジェクトに対して、
    from_dict(to_dict(survey))は元のオブジェクトと等価なオブジェクトを生成する。

    **Validates: Requirements 11.2**
    """
    restored = Survey.from_dict(survey.to_dict())

    assert restored.id == survey.id
    assert restored.name == survey.name
    assert restored.description == survey.description
    assert restored.template_id == survey.template_id
    assert restored.persona_count == survey.persona_count
    assert restored.filters == survey.filters
    assert restored.status == survey.status
    assert restored.s3_result_path == survey.s3_result_path
    assert restored.created_at == survey.created_at
    assert restored.updated_at == survey.updated_at
    assert restored.error_message == survey.error_message

    if survey.insight_report is None:
        assert restored.insight_report is None
    else:
        assert restored.insight_report is not None
        assert restored.insight_report.id == survey.insight_report.id
        assert restored.insight_report.survey_id == survey.insight_report.survey_id
        assert restored.insight_report.content == survey.insight_report.content
        assert restored.insight_report.created_at == survey.insight_report.created_at
