/**
 * アンケートのステートマシンの設定と定義（基盤設計 2.2、functional-spec.md WF5）。
 *
 * ここは CDK の構成部品を作らない純粋な関数だけを置く（test/app-stack.test.ts で形を固定する）。
 * 資源を作るのは constructs/survey-run-state-machine.ts。
 *
 * Distributed Map（ItemReader は S3 の JSONL、子の実行は Express）から bedrock:invokeModel を呼ぶ。
 * 入力と結果の場所はアンケートごとに変わるため、ItemReader と ResultWriter のバケット・キー・prefix を
 * 実行の入力（JSONPath）から取る。CDK 2.265.0 の S3JsonLItemReader と ResultWriterV2 はキーと
 * prefix を固定の文字列にしかできないため、ASL を直接書く（基盤設計のレビュー R-02）。
 */

/** 1 件の実行の並列数の既定と範囲（BR2.6、NFR3.4）。 */
export const DEFAULT_SURVEY_MAX_CONCURRENCY = 64;
const MAX_CONCURRENCY_RANGE = { min: 1, max: 1000 } as const;
/** 1 件の実行の打ち切り時間（秒）の既定と範囲（BR3.3、NFR3.5）。 */
export const DEFAULT_SURVEY_EXECUTION_TIMEOUT_SECONDS = 7200;
const EXECUTION_TIMEOUT_RANGE = { min: 600, max: 86_400 } as const;

/**
 * 1 回の推論の打ち切り（秒）。Express の子の実行は 5 分まで。再試行を含めた最悪
 * （7 回 × 30 秒 + 待ちの上限の合計 62 秒 = 272 秒）でも収まる（NFR3.15、基盤設計のレビュー R-01）。
 */
export const INVOKE_TIMEOUT_SECONDS = 30;

/**
 * 一時的なエラーの再試行（BR3.5 に MaxDelaySeconds 16 を足した、基盤設計 2.2）。
 *
 * 対象は InvokeModel の例外のうち、時間をおけば通りうるものだけ（Step Functions の統合では
 * `Bedrock.<例外名>`）。ValidationException・AccessDeniedException・ResourceNotFoundException・
 * ServiceQuotaExceededException・ModelErrorException は、同じ依頼をやり直しても直らないので含めない。
 * 対象を足しても回数（MaxAttempts）は変わらないので、最悪の時間は変わらない（NFR3.15）。
 */
export const INVOKE_RETRY = Object.freeze({
  ErrorEquals: [
    'Bedrock.ThrottlingException',
    'Bedrock.ServiceUnavailableException',
    'Bedrock.ModelNotReadyException',
    'Bedrock.InternalServerException',
    'Bedrock.ModelTimeoutException',
    'States.Timeout',
  ],
  IntervalSeconds: 2,
  BackoffRate: 2,
  MaxAttempts: 6,
  MaxDelaySeconds: 16,
  JitterStrategy: 'FULL',
});

/** The values a deployment chooses (parameters.ts, both optional). */
export interface SurveyRunSettings {
  readonly maxConcurrency: number;
  readonly timeoutSeconds: number;
}

/**
 * The parallelism and the time limit, defaulted and checked (NFR3.4, NFR3.5).
 *
 * Throws an Error naming the parameter when a value is not an integer in its range, so a
 * wrong value stops `cdk synth` instead of reaching the state machine.
 */
export function resolveSurveyRunSettings(input: {
  surveyMaxConcurrency?: number;
  surveyExecutionTimeoutSeconds?: number;
}): SurveyRunSettings {
  return {
    maxConcurrency: integerIn(
      'surveyMaxConcurrency',
      input.surveyMaxConcurrency ?? DEFAULT_SURVEY_MAX_CONCURRENCY,
      MAX_CONCURRENCY_RANGE,
    ),
    timeoutSeconds: integerIn(
      'surveyExecutionTimeoutSeconds',
      input.surveyExecutionTimeoutSeconds ?? DEFAULT_SURVEY_EXECUTION_TIMEOUT_SECONDS,
      EXECUTION_TIMEOUT_RANGE,
    ),
  };
}

function integerIn(name: string, value: number, range: { min: number; max: number }): number {
  if (!Number.isInteger(value) || value < range.min || value > range.max) {
    throw new Error(`${name} must be an integer from ${range.min} to ${range.max}, got "${value}"`);
  }
  return value;
}

/** One persona's InvokeModel Task: the body in the item, or in S3 (BR6.3). */
function invokeTask(model: Record<string, unknown>): Record<string, unknown> {
  return {
    Type: 'Task',
    Resource: 'arn:aws:states:::bedrock:invokeModel',
    Parameters: {
      'ModelId.$': '$.model_id',
      ...model,
      ContentType: 'application/json',
      Accept: 'application/json',
    },
    // 応答の本文だけを残す（依頼の本文を出力に複製しない、functional-spec.md 6 章）
    ResultSelector: { 'content.$': '$.Body.content' },
    TimeoutSeconds: INVOKE_TIMEOUT_SECONDS,
    Retry: [{ ...INVOKE_RETRY, ErrorEquals: [...INVOKE_RETRY.ErrorEquals] }],
    End: true,
  };
}

/**
 * The state machine definition (ASL) of one survey execution (WF5).
 *
 * The execution input is `{ bucket, input_key, result_prefix }` (functional-spec.md 6章). Item
 * failures never fail the execution (ToleratedFailurePercentage 100, BR3.6): the app judges the
 * 5% rule from the ResultWriter output (BR4.1).
 */
export function surveyRunDefinition(settings: SurveyRunSettings): Record<string, unknown> {
  return {
    Comment: 'AI Persona survey: one Bedrock InvokeModel per persona (Distributed Map)',
    StartAt: 'AnswerSurvey',
    TimeoutSeconds: settings.timeoutSeconds,
    States: {
      AnswerSurvey: {
        Type: 'Map',
        ItemReader: {
          Resource: 'arn:aws:states:::s3:getObject',
          ReaderConfig: { InputType: 'JSONL' },
          Parameters: { 'Bucket.$': '$.bucket', 'Key.$': '$.input_key' },
        },
        ItemProcessor: {
          ProcessorConfig: { Mode: 'DISTRIBUTED', ExecutionType: 'EXPRESS' },
          StartAt: 'BodyInS3',
          States: {
            BodyInS3: {
              Type: 'Choice',
              Choices: [{ Variable: '$.body_s3_uri', IsPresent: true, Next: 'InvokeWithBodyInS3' }],
              Default: 'InvokeWithBody',
            },
            InvokeWithBody: invokeTask({ 'Body.$': '$.body' }),
            InvokeWithBodyInS3: invokeTask({ Input: { 'S3Uri.$': '$.body_s3_uri' } }),
          },
        },
        MaxConcurrency: settings.maxConcurrency,
        ToleratedFailurePercentage: 100,
        ResultWriter: {
          Resource: 'arn:aws:states:::s3:putObject',
          Parameters: { 'Bucket.$': '$.bucket', 'Prefix.$': '$.result_prefix' },
        },
        End: true,
      },
    },
  };
}
