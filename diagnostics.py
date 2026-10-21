"""Safe operational metadata; never log request text, model content or user IDs."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
import json
import logging
import os
import time
from uuid import uuid4

LOG = logging.getLogger('operator_diagnostics')


class PipelineError(ValueError):
    def __init__(self, code, public_message, source_count=None, extracted_count=None):
        self.code, self.public_message = code, public_message
        self.source_count, self.extracted_count = source_count, extracted_count
        super().__init__(public_message)


class SemanticExtractionError(PipelineError):
    """Eligible for one more extraction; never a transport/schema/program error."""


@dataclass
class Trace:
    operation_id: str = field(default_factory=lambda:uuid4().hex[:12])
    stage: str = 'routing'
    calls: int = 0
    stage_calls: dict = field(default_factory=dict)
    model: str | None = None
    reasoning: str | None = None
    medium: bool = False
    source_count: int | None = None
    extracted_count: int | None = None
    started: float = field(default_factory=time.monotonic)
    phase_started: float = field(default_factory=time.monotonic)
    stage_ms: dict = field(default_factory=dict)
    http_attempts: int = 0
    sdk_retries: int = 0
    http_request_bytes: int | None = None
    result: str = 'ok'


CURRENT = ContextVar('operator_trace',default=None)


def phase(stage, source_count=None, extracted_count=None):
    trace = CURRENT.get()
    if trace:
        now = time.monotonic()
        trace.stage_ms[trace.stage] = trace.stage_ms.get(trace.stage, 0) + (now-trace.phase_started)*1000
        trace.phase_started = now
        trace.stage=stage
        if source_count is not None:
            trace.source_count=source_count
        if extracted_count is not None:
            trace.extracted_count=extracted_count


def emit(event, **fields):
    LOG.info(json.dumps({'event':event,**fields},ensure_ascii=False))


def http_attempt(request):
    """Public SDK hook: counts only, never body, URLs, headers or credentials."""
    trace = CURRENT.get()
    if trace:
        trace.http_attempts += 1
        try:
            retry = int(request.headers.get('x-stainless-retry-count', '0'))
        except (TypeError, ValueError):
            retry = 0
        trace.sdk_retries += int(retry > 0)
        try:
            trace.http_request_bytes = len(request.content)
        except Exception:
            # Do not consume a streaming body or fail an API call for telemetry.
            trace.http_request_bytes = None


def safe_code(error):
    if isinstance(error,PipelineError):
        return error.code
    name=type(error).__name__
    return {'ValidationError':'schema_validation','APIConnectionError':'api_connection',
        'APITimeoutError':'api_timeout','AuthenticationError':'api_authentication',
        'PermissionDeniedError':'api_permission','RateLimitError':'api_rate_limit',
        'BadRequestError':'api_bad_request','NotFoundError':'api_model_not_found',
        'LengthFinishReasonError':'api_response_length','ContentFilterFinishReasonError':'api_content_filter',
        'PermissionError':'access_denied'}.get(name,'program_error' if not isinstance(error,ValueError) else 'validation_error')


def record_error(error):
    trace = CURRENT.get()
    if trace is None:
        return
    trace.result = safe_code(error)
    if isinstance(error, PipelineError):
        if error.source_count is not None:
            trace.source_count = error.source_count
        if error.extracted_count is not None:
            trace.extracted_count = error.extracted_count
    emit('processing_error', operation=trace.operation_id, stage=trace.stage,
         exception=type(error).__name__, reason=trace.result, source_count=trace.source_count,
         extracted_count=trace.extracted_count, calls=trace.calls, medium=trace.medium,
         model=trace.model or os.getenv('OPENAI_MODEL','gpt-6-luna'),
         reasoning=trace.reasoning or os.getenv('OPENAI_REASONING_EFFORT','low'))


@contextmanager
def operation():
    trace=Trace()
    token=CURRENT.set(trace)
    try:
        yield trace
    except Exception as error:
        record_error(error)
        raise
    finally:
        phase(trace.stage)
        emit('operation_complete',operation=trace.operation_id,stage=trace.stage,result=trace.result,
             calls=trace.calls,stage_calls=trace.stage_calls,medium=trace.medium,
             stage_ms={key:round(value,3) for key,value in trace.stage_ms.items()},
             http_attempts=trace.http_attempts,sdk_retries=trace.sdk_retries,
             duration_ms=round((time.monotonic()-trace.started)*1000,3))
        CURRENT.reset(token)


def usage_metadata(response):
    usage=getattr(response,'usage',None)
    result={}
    for key in ['prompt_tokens','completion_tokens','total_tokens']:
        value=getattr(usage,key,None)
        if isinstance(value,int):
            result[key]=value
    for owner,key,label in [(getattr(usage,'prompt_tokens_details',None),'cached_tokens','cached_tokens'),
                            (getattr(usage,'completion_tokens_details',None),'reasoning_tokens','reasoning_tokens')]:
        value=getattr(owner,key,None)
        if isinstance(value,int):
            result[label]=value
    input_rate=os.getenv('OPENAI_INPUT_USD_PER_MILLION')
    output_rate=os.getenv('OPENAI_OUTPUT_USD_PER_MILLION')
    cached_rate=os.getenv('OPENAI_CACHED_INPUT_USD_PER_MILLION',input_rate)
    if input_rate and output_rate and 'prompt_tokens' in result and 'completion_tokens' in result:
        try:
            cached=result.get('cached_tokens',0)
            cost=((result['prompt_tokens']-cached)*Decimal(input_rate)+cached*Decimal(cached_rate)+result['completion_tokens']*Decimal(output_rate))/Decimal(1_000_000)
            result['estimated_usd']=str(cost)
        except Exception:
            result['estimated_usd']=None
    return result
