"""How a failed call to an external service is handled (reliability 3.1).

The survey runner treats every failed external call the same way whichever
stage it happens in (starting, monitoring, finishing). The Service layer turns
an SDK error into one of four classes and the Manager decides what to do.
"""

from enum import StrEnum


class FailureClass(StrEnum):
    """The four classes of reliability-requirements.md 3.1."""

    #: The execution or its input / output is not there; waiting will not help.
    NOT_FOUND = "not_found"
    #: The survey's own content is wrong (bad input, unreadable data).
    INVALID = "invalid"
    #: Permissions or configuration of the deployment (not the survey's fault).
    ACCESS = "access"
    #: May work later. Anything that cannot be classified is treated as this.
    TRANSIENT = "transient"


#: SDK error codes of Step Functions, S3 and the credential chain
#: (reliability-requirements.md 3.1). Codes not listed are TRANSIENT, so a
#: wrong guess still ends at a count or time limit (BR3.2, BR3.7, NFR4.7).
_FAILURE_BY_CODE: dict[str, FailureClass] = {
    # Not found
    "ExecutionDoesNotExist": FailureClass.NOT_FOUND,
    "InvalidArn": FailureClass.NOT_FOUND,
    "NoSuchKey": FailureClass.NOT_FOUND,
    "404": FailureClass.NOT_FOUND,
    "NotFound": FailureClass.NOT_FOUND,
    # The survey's own input
    "InvalidExecutionInput": FailureClass.INVALID,
    "InvalidName": FailureClass.INVALID,
    # Permissions or configuration
    "AccessDeniedException": FailureClass.ACCESS,
    "AccessDenied": FailureClass.ACCESS,
    "403": FailureClass.ACCESS,
    "StateMachineDoesNotExist": FailureClass.ACCESS,
    "StateMachineDeleting": FailureClass.ACCESS,
    "NoSuchBucket": FailureClass.ACCESS,
    "UnrecognizedClientException": FailureClass.ACCESS,
    "InvalidAccessKeyId": FailureClass.ACCESS,
    "ExpiredToken": FailureClass.ACCESS,
    "ExpiredTokenException": FailureClass.ACCESS,
    "InvalidClientTokenId": FailureClass.ACCESS,
}


def classify_aws_error(code: str) -> FailureClass:
    """The class of an AWS SDK error code (``KMS.*`` codes are ACCESS)."""
    if code.startswith("KMS."):
        return FailureClass.ACCESS
    return _FAILURE_BY_CODE.get(code, FailureClass.TRANSIENT)
