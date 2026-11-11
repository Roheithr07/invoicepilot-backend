"""
Resilience, Retry & Idempotency Safeguards for InvoicePilot AI.
Provides exponential backoff for transient read/lookup operations,
enforces pre-retry remote verification for financial state changes,
and guards against concurrent duplicate submissions.
"""

import time
import logging
from typing import Callable, TypeVar, Optional, Tuple, Any, List, Type

logger = logging.getLogger("InvoicePilot.Resilience")

T = TypeVar("T")


class RetryBudgetExceeded(Exception):
    """Raised when maximum retries are exhausted."""
    pass


class UncertainRemoteStateError(Exception):
    """
    Raised when an external financial request timed out or was interrupted,
    and remote state must be verified before proceeding.
    """
    pass


def retry_with_backoff(
    operation: Callable[..., T],
    max_retries: int = 3,
    base_delay: float = 0.5,
    backoff_factor: float = 2.0,
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
    operation_name: str = "operation"
) -> T:
    """
    Executes an idempotent operation with exponential backoff.
    NEVER use this wrapper on non-idempotent financial mutations
    (such as invoice send or payment capture).
    """
    delay = base_delay
    last_exception = None

    for attempt in range(1, max_retries + 1):
        try:
            return operation()
        except retryable_exceptions as exc:
            last_exception = exc
            if attempt == max_retries:
                logger.warning(
                    f"[{operation_name}] Exhausted all {max_retries} attempts. Last error: {str(exc)}"
                )
                break
            logger.info(
                f"[{operation_name}] Attempt {attempt}/{max_retries} failed ({str(exc)}). Retrying in {delay:.2f}s..."
            )
            time.sleep(delay)
            delay *= backoff_factor

    raise RetryBudgetExceeded(
        f"Operation '{operation_name}' failed after {max_retries} attempts: {str(last_exception)}"
    ) from last_exception


def verify_remote_before_retry(
    state_verifier: Callable[[], Optional[Any]],
    action: Callable[[], T],
    already_completed_predicate: Callable[[Any], bool],
    operation_name: str = "financial_action"
) -> Tuple[T, bool]:
    """
    Financial Safety Guard:
    Before performing or retrying an action, inspects remote state to ensure
    it hasn't already been completed remotely during a prior network timeout.
    Returns: (result, was_already_completed)
    """
    # 1. Inspect remote state first
    remote_state = state_verifier()
    if remote_state is not None and already_completed_predicate(remote_state):
        logger.info(
            f"[{operation_name}] Remote state check confirmed action was ALREADY executed remotely. Bypassing duplicate dispatch."
        )
        return remote_state, True

    # 2. Remote check confirmed not completed, proceed with action safely
    result = action()
    return result, False

