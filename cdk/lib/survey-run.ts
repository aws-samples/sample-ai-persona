/**
 * Settings and definition of the survey state machine.
 *
 * Only pure functions live here, with no CDK constructs (test/app-stack.test.ts pins the shape).
 * The resources are created in constructs/survey-run-state-machine.ts.
 *
 * A Distributed Map (ItemReader reads JSONL from S3, child executions are Express) calls
 * bedrock:invokeModel. The input and result locations differ per survey, so the ItemReader and
 * ResultWriter bucket, key and prefix come from the execution input (JSONPath). CDK 2.265.0's
 * S3JsonLItemReader and ResultWriterV2 only accept fixed strings for key and prefix, so the ASL
 * is written directly.
 */

/** Default and allowed range of parallelism for one execution. */
export const DEFAULT_SURVEY_MAX_CONCURRENCY = 64;
const MAX_CONCURRENCY_RANGE = { min: 1, max: 1000 } as const;
/** Default and allowed range of the execution time limit in seconds. */
export const DEFAULT_SURVEY_EXECUTION_TIMEOUT_SECONDS = 7200;
const EXECUTION_TIMEOUT_RANGE = { min: 600, max: 86_400 } as const;

/**
 * Time limit of one inference in seconds. An Express child execution lasts at most 5 minutes;
 * the worst case with retries (7 attempts x 30 s + 62 s of waits = 272 s) still fits.
 */
export const INVOKE_TIMEOUT_SECONDS = 30;

/**
 * Retry for transient errors.
 *
 * Only InvokeModel exceptions that may succeed after a pause are listed (`Bedrock.<name>` in the
 * Step Functions integration). ValidationException, AccessDeniedException,
 * ResourceNotFoundException, ServiceQuotaExceededException and ModelErrorException are excluded
 * because repeating the same request cannot fix them. Adding error types does not change
 * MaxAttempts, so the worst-case duration is unchanged.
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
 * The parallelism and the time limit, defaulted and checked.
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

/** One persona's InvokeModel Task: the body in the item, or in S3. */
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
    // Keep only the response body so the request body is not copied into the output
    ResultSelector: { 'content.$': '$.Body.content' },
    TimeoutSeconds: INVOKE_TIMEOUT_SECONDS,
    Retry: [{ ...INVOKE_RETRY, ErrorEquals: [...INVOKE_RETRY.ErrorEquals] }],
    End: true,
  };
}

/**
 * The state machine definition (ASL) of one survey execution.
 *
 * The execution input is `{ bucket, input_key, result_prefix }`. Item
 * failures never fail the execution (ToleratedFailurePercentage 100): the app judges the
 * 5% failure rule from the ResultWriter output.
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
