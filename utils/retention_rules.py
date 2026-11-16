"""
The decisions of the retention policy that involve no database and no clock of their own, so they can be tested to the letter.

The policy deletes archived messages older than N months (see db/queries.py, "Retention", and retention.py for the job that runs it).
Deleting is irreversible, so two independent safeguards sit between "a number was saved" and "messages are gone", and both live here:

1. The CONFIRMATION.
   When the owner saves the policy, the page shows how many messages it would delete right now and sends that number back.
   The server counts again;
   if the real count is clearly higher than what was confirmed (the page was stale, or the form changed after the preview), the save is refused.
   The confirmed number is then remembered, and the first run may delete about that many, no more.
2. The STEADY-STATE LIMIT.
   After the confirmed first run, a normal run only removes what aged past the cutoff since the last one: a day's worth.
   A run that would delete far more than that (the server clock jumped, the archiver was stopped for months)
   is BLOCKED and waits for the owner to look at the new preview and save again.
   A blocked run deletes nothing.

Everything here is a pure function of its arguments.
"""

from datetime import datetime, timedelta

from utils.validation import (
    RETENTION_CONFIRM_TOLERANCE_FRACTION,
    RETENTION_CONFIRM_TOLERANCE_MIN,
    RETENTION_RUN_INTERVAL_HOURS,
    RETENTION_STEADY_RUN_FRACTION,
    RETENTION_STEADY_RUN_MIN,
)


def confirmation_tolerance(confirmed: int) -> int:
    """
    How many messages beyond the confirmed count are still "the same thing the owner agreed to".
    Messages keep ageing past the cutoff between the preview and the run, so an exact match would refuse almost every save on a busy archive.
    """
    return max(RETENTION_CONFIRM_TOLERANCE_MIN, int(confirmed * RETENTION_CONFIRM_TOLERANCE_FRACTION))


def run_limit(confirmed: int | None, total_messages: int) -> int:
    """
    The most messages a run may delete.

    `confirmed` is the count the owner confirmed when saving and is set until the first run completes: that run may delete about that many.
    Afterwards (`confirmed` is None) the limit is the steady-state one: a fixed floor or a share of the whole archive, whichever is larger.
    """
    if confirmed is not None:
        return confirmed + confirmation_tolerance(confirmed)
    return max(RETENTION_STEADY_RUN_MIN, int(total_messages * RETENTION_STEADY_RUN_FRACTION))


def preview_is_stale(confirmed: int, actual: int) -> bool:
    """True when saving would delete clearly more than the owner confirmed - the save must be refused and the page must preview again."""
    return actual > confirmed + confirmation_tolerance(confirmed)


def run_is_due(
    *,
    last_run_at: datetime | None,
    last_run_status: str | None,
    updated_at: datetime,
    now: datetime,
) -> bool:
    """
    Whether the archiver should apply the policy now (only ever asked for an ENABLED policy).

    - Never run: due.
      Saving the policy again (`updated_at` after the last run) is due at once: the owner changed it or confirmed a blocked run.
    - A BLOCKED run stays blocked until the policy is saved again; asking every day would only repeat the same refusal.
    - Otherwise once every RETENTION_RUN_INTERVAL_HOURS (an error is retried at that pace too, never in a tight loop).
    """
    if last_run_at is None or updated_at > last_run_at:
        return True
    if last_run_status == "blocked":
        return False
    return now - last_run_at >= timedelta(hours=RETENTION_RUN_INTERVAL_HOURS)
