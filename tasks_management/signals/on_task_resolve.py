import datetime
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from simple_history.utils import bulk_create_with_history

from core.forms import User
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task, TaskDecision, TaskExecutor, TaskFlowStep
from tasks_management.services import TaskService, TaskCompletionRefused

logger = logging.getLogger(__name__)

# Whole-task verdicts accepted on the flow path. REJECTED is reserved for
# per-record verdicts (flat batch tasks); rewind semantics arrive with v1.1.
_FLOW_VERDICTS = (TaskDecision.Decision.APPROVED, TaskDecision.Decision.FAILED)


def _complete_task(_task, _user, failed=False):
    payload = {"id": _task.id, 'failed': True} if failed else {"id": _task.id}
    result = TaskService(_user).complete_task(payload)
    if not result.get('success'):
        logger.error("Completion of task %s refused: %s", _task.id, result.get('detail'))
        raise TaskCompletionRefused(result)
    return result


def resolve_task_all(_task, _user):
    if 'FAILED' in _task.business_status.values():
        _complete_task(_task, _user, failed=True)
    n_of_approves = sum(map('APPROVED'.__eq__, _task.business_status.values()))
    n_of_executors = _task.task_group.taskexecutor_set.filter(task_group__is_deleted=False, is_deleted=False).count()
    if not n_of_executors:
        logger.warning("No valid executors of task with policy ALL %s", str(_task.uuid))
    if n_of_approves == n_of_executors:
        _complete_task(_task, _user)


def resolve_task_any(_task, _user):
    if 'FAILED' in _task.business_status.values():
        _complete_task(_task, _user, failed=True)
    if 'APPROVED' in _task.business_status.values():
        _complete_task(_task, _user)


def resolve_task_n(_task, _user):
    """
    N-of-M: complete once `threshold` executors have approved.

    This previously delegated to resolve_task_any, so an N group completed on
    the first approval. That was invisible while there was no threshold to
    set; now that TaskGroup.threshold is part of the public API (and required
    for N by a DB constraint), leaving it unused would ship a setting that
    demonstrably does nothing. A group that somehow has no threshold holds
    rather than silently falling back to ANY - the group is misconfigured,
    and approving on one vote is the least safe reading of "N".
    """
    if 'FAILED' in _task.business_status.values():
        _complete_task(_task, _user, failed=True)
        return
    threshold = _task.task_group.threshold
    if not threshold:
        logger.error(
            "tasks_management: task %s has policy N but its group '%s' has no "
            "threshold; holding", _task.id, _task.task_group.code,
        )
        return
    n_of_approves = sum(map('APPROVED'.__eq__, _task.business_status.values()))
    if n_of_approves >= threshold:
        _complete_task(_task, _user)


def _record_flat_decisions(_task, _user, _verdict):
    """
    Insert-only ledger mirror of the caller's flat-task vote. Additive
    observability for flat tasks: a failure here must never block the
    legacy resolution path, so errors are logged loudly and swallowed.
    """
    try:
        if isinstance(_verdict, str):
            if _verdict not in _FLOW_VERDICTS:
                return
            exists = TaskDecision.objects.filter(
                task=_task, flow_step__isnull=True, user=_user,
                record_id__isnull=True, is_deleted=False,
            ).exists()
            if not exists:
                TaskDecision(
                    task=_task, user=_user, decision=_verdict,
                ).save(username=_user.login_name)
        elif isinstance(_verdict, dict):
            # A CSV import can carry thousands of rows; one exists() query and
            # one save() per record would turn a single approval into
            # thousands of round trips. Load this user's existing record ids
            # for this task once, then bulk_create only the new ones -
            # set_pk()/user_created/date_created replicate what save() does
            # on first insert (this is an insert-only ledger, so there is no
            # update path to also cover).
            # Flat tasks never reach _validate_flow_vote, so the record lists
            # are shaped here: a bare string is iterable and would otherwise
            # be mirrored as one decision per character.
            per_record = (
                (TaskDecision.Decision.APPROVED, _verdict.get('ACCEPT') or []),
                (TaskDecision.Decision.REJECTED, _verdict.get('REJECT') or []),
            )
            if any(not isinstance(ids, (list, tuple)) for _, ids in per_record):
                logger.error(
                    "tasks_management: per-record vote on flat task %s has a "
                    "non-list ACCEPT/REJECT; not mirrored into TaskDecision",
                    _task.id,
                )
                return
            existing_ids = set(TaskDecision.objects.filter(
                task=_task, flow_step__isnull=True, user=_user, is_deleted=False,
            ).values_list('record_id', flat=True))
            now = datetime.datetime.now()
            new_decisions = []
            seen_ids = set()
            for decision, record_ids in per_record:
                for record_id in record_ids:
                    record_id = str(record_id)
                    if not record_id.strip() or record_id in existing_ids or record_id in seen_ids:
                        continue
                    seen_ids.add(record_id)
                    row = TaskDecision(
                        task=_task, user=_user, decision=decision, record_id=record_id,
                        user_created=_user, user_updated=_user,
                        date_created=now, date_updated=now,
                    )
                    row.set_pk()
                    new_decisions.append(row)
            if new_decisions:
                # bulk_create emits no post_save, and HistoryModel's history is
                # simple-history (signal driven) - a plain bulk_create would
                # leave these rows with no HistoricalTaskDecision entry while
                # whole-task decisions still get one.
                bulk_create_with_history(new_decisions, TaskDecision)
    except Exception as exc:
        logger.error(
            "tasks_management.flow: failed to mirror flat vote into TaskDecision "
            "for task %s user %s", _task.id, _user.id, exc_info=exc,
        )


def _is_privileged_resolver(_user):
    # Mirrors the visibility rule in gql_queries: imis admins and task-triage
    # users see (and may act on) every task.
    from tasks_management.gql_queries import is_task_triage
    return getattr(_user, 'is_imis_admin', False) or is_task_triage(_user)


def _is_batch_task(_task):
    """
    Batch sources submit {ACCEPT: [...], REJECT: [...]} per record instead of
    one verdict for the whole task, so they evaluate on the batch path.
    """
    return _task.source in (TasksManagementConfig.flow_batch_sources or [])


def _validate_flow_vote(_task, _step, _user, _verdict):
    """
    Shape validation and membership are independent gates - shape decides
    which kind of verdict this task accepts, membership decides who may cast
    one. A direct TaskService.resolve_task caller (GraphQL enforces this
    separately, but is not the only entry point) must pass both regardless of
    which shape branch it took.
    """
    if isinstance(_verdict, dict):
        if not _is_batch_task(_task):
            raise ValidationError(
                "tasks_management.flow: source '%s' submitted a per-record vote but "
                "is not a batch source - add '%s' to flow_batch_sources, or remove "
                "the flow binding" % (_task.source, _task.source)
            )
        # Shape the record lists strictly rather than relying on truthiness:
        # a bare string is iterable, so it would otherwise be recorded as one
        # decision per character, and an id in both lists would silently
        # resolve to whichever list is read first.
        accepted, rejected = _verdict.get('ACCEPT'), _verdict.get('REJECT')
        for name, ids in (('ACCEPT', accepted), ('REJECT', rejected)):
            if ids is not None and not isinstance(ids, (list, tuple)):
                raise ValidationError(
                    "tasks_management.flow: '%s' in a per-record vote on task '%s' "
                    "must be a list of record ids, got %s"
                    % (name, _task.id, type(ids).__name__)
                )
            if any(not str(record_id).strip() for record_id in (ids or [])):
                raise ValidationError(
                    "tasks_management.flow: '%s' in a per-record vote on task '%s' "
                    "contains a blank record id" % (name, _task.id)
                )
        accepted_ids = {str(record_id) for record_id in (accepted or [])}
        rejected_ids = {str(record_id) for record_id in (rejected or [])}
        if not (accepted_ids or rejected_ids):
            raise ValidationError(
                "tasks_management.flow: per-record vote on task '%s' names no "
                "records" % _task.id
            )
        overlap = accepted_ids & rejected_ids
        if overlap:
            raise ValidationError(
                "tasks_management.flow: per-record vote on task '%s' both accepts "
                "and rejects record(s) %s" % (_task.id, ", ".join(sorted(overlap)))
            )
    else:
        if _is_batch_task(_task):
            raise ValidationError(
                "tasks_management.flow: source '%s' is a batch source and must submit "
                "a per-record vote, not '%s'" % (_task.source, _verdict)
            )
        if _verdict not in _FLOW_VERDICTS:
            raise ValidationError(
                "tasks_management.flow: unsupported verdict '%s' for a flow task - "
                "accepted values: %s" % (_verdict, ", ".join(_FLOW_VERDICTS))
            )

    is_executor = TaskExecutor.objects.filter(
        task_group_id=_step.task_group_id, user=_user,
        is_deleted=False, task_group__is_deleted=False,
    ).exists()
    if not is_executor and not _is_privileged_resolver(_user):
        raise ValidationError(
            "tasks_management.flow: user '%s' is not an executor of group '%s' for "
            "the current step and has no triage rights" % (_user.id, _step.task_group.code)
        )


def _advance_or_complete(_task, _step, _user):
    """
    Move a task off a passed step: to the next step of its pinned flow
    version, or 'completed' when the passed step was terminal. Shared by the
    whole-task and batch evaluators - advancement does not depend on how the
    step was judged.
    """
    # Advance within the task's pinned flow version - steps are read through
    # the FK only, never re-resolved through the flow code (version pinning).
    next_step = TaskFlowStep.objects.filter(
        flow_id=_task.flow_id, is_deleted=False, order__gt=_step.order,
    ).select_related('task_group').order_by('order').first()

    if next_step is None:
        # Terminal step passed. current_step deliberately keeps pointing at
        # the terminal step so the FE stepper can render the finished state.
        logger.info(
            "tasks_management.flow: task %s passed terminal step %s (order %s)",
            _task.id, _step.id, _step.order,
        )
        return 'completed'

    _task.current_step = next_step
    # Repointing task_group keeps executor-based visibility, counting and the
    # FE working unchanged: the task now belongs to the next step's pool.
    _task.task_group = next_step.task_group
    _task.save(username=_user.login_name)
    logger.info(
        "tasks_management.flow: task %s advanced from step order %s to %s (pool '%s')",
        _task.id, _step.order, next_step.order, next_step.task_group.code,
    )
    next_pool_size = next_step.task_group.taskexecutor_set.filter(
        task_group__is_deleted=False, is_deleted=False,
    ).count()
    if next_pool_size == 0:
        logger.error(
            "tasks_management.flow: task %s advanced into empty pool '%s' (order %s) "
            "- held until the pool is refilled",
            _task.id, next_step.task_group.code, next_step.order,
        )
    return None


def _record_batch_decisions(_task, _step, _user, _verdict):
    """
    Insert this reviewer's per-record verdicts for the current step. Re-votes
    on a record already decided by the same reviewer at this step are ignored
    (check-then-insert under the caller's lock, mirroring the whole-task path:
    an IntegrityError inside the atomic block would poison the transaction).

    This runs inside resolve_flow_task's select_for_update block, so one
    query to load existing ids plus one bulk_create - instead of a query and
    a save() per record - matters even more here than in the flat path: a CSV
    approval with thousands of rows would otherwise hold the task's row lock
    for the length of thousands of round trips, blocking every other voter.

    Returns True when at least one new decision was recorded.
    """
    per_record = (
        (TaskDecision.Decision.APPROVED, _verdict.get('ACCEPT') or []),
        (TaskDecision.Decision.REJECTED, _verdict.get('REJECT') or []),
    )
    existing_ids = set(TaskDecision.objects.filter(
        task=_task, flow_step=_step, user=_user, is_deleted=False,
    ).values_list('record_id', flat=True))
    now = datetime.datetime.now()
    new_decisions = []
    seen_ids = set()
    for decision, record_ids in per_record:
        for record_id in record_ids:
            record_id = str(record_id)
            if not record_id or record_id in existing_ids or record_id in seen_ids:
                continue
            seen_ids.add(record_id)
            row = TaskDecision(
                task=_task, flow_step=_step, user=_user,
                decision=decision, record_id=record_id,
                user_created=_user, user_updated=_user,
                date_created=now, date_updated=now,
            )
            row.set_pk()
            new_decisions.append(row)
    if new_decisions:
        # See _record_flat_decisions: bulk_create bypasses simple-history.
        bulk_create_with_history(new_decisions, TaskDecision)
    recorded = bool(new_decisions)
    return recorded


def flow_rejected_record_ids(_task):
    """
    Record ids rejected at any step of this task's flow.

    Consumer modules apply the batch once, at completion, to everything in the
    upload except these - mirroring the flat behaviour, where a rejected row is
    removed and whatever is left gets imported. Deliberately the complement:
    tasks_management never sees the universe of records, only the verdicts, so
    the caller subtracts rather than us enumerating.
    """
    return set(
        TaskDecision.objects.filter(
            task=_task, flow_step__isnull=False,
            decision=TaskDecision.Decision.REJECTED, is_deleted=False,
        ).exclude(record_id__isnull=True).values_list('record_id', flat=True)
    )


def _evaluate_batch_step(_task, _step, _user):
    """
    Step evaluation for a batch task. The batch advances as a unit, so the
    step's policy counts *reviewers who have submitted a verdict* rather than
    approvals of a single subject - there is no one verdict to count. Records
    rejected here stay rejected for the rest of the flow; the surviving set is
    derived from the ledger at completion.

    Returns 'completed' or None. A per-record rejection never fails the whole
    task: it drops that row, which is what the flat path does too.
    """
    reviewers = TaskDecision.objects.filter(
        task=_task, flow_step=_step, is_deleted=False,
    ).exclude(record_id__isnull=True).values('user').distinct().count()

    executor_count = _step.task_group.taskexecutor_set.filter(
        task_group__is_deleted=False, is_deleted=False,
    ).count()
    if executor_count == 0:
        logger.error(
            "tasks_management.flow: batch task %s held at step %s (order %s) - "
            "executor pool '%s' is empty; refill the pool to resume",
            _task.id, _step.id, _step.order, _step.task_group.code,
        )
        return None

    policy = _step.effective_policy()
    threshold = _step.effective_threshold()
    if policy == TaskFlowStep.StepCompletionPolicy.ALL:
        passed = reviewers >= executor_count
    elif policy == TaskFlowStep.StepCompletionPolicy.ANY:
        passed = reviewers >= 1
    elif policy == TaskFlowStep.StepCompletionPolicy.N:
        if not threshold:
            logger.error(
                "tasks_management.flow: batch task %s step %s has policy N without "
                "a threshold; holding", _task.id, _step.id,
            )
            return None
        passed = reviewers >= threshold
    else:
        logger.error(
            "tasks_management.flow: batch task %s step %s has unknown policy '%s'; "
            "holding", _task.id, _step.id, policy,
        )
        return None

    if not passed:
        return None

    return _advance_or_complete(_task, _step, _user)


def _evaluate_step(_task, _step, _user):
    """
    Evaluate the current step under the caller's task row lock.
    Returns 'failed', 'completed', or None (step still open / advanced).
    """
    decisions = TaskDecision.objects.filter(
        task=_task, flow_step=_step, record_id__isnull=True, is_deleted=False,
    )
    # Any FAILED verdict fails the whole task - explicit short-circuit, no
    # fall-through to the approval branch.
    if decisions.filter(decision=TaskDecision.Decision.FAILED).exists():
        logger.info(
            "tasks_management.flow: task %s FAILED at step %s (order %s)",
            _task.id, _step.id, _step.order,
        )
        return 'failed'

    executor_count = _step.task_group.taskexecutor_set.filter(
        task_group__is_deleted=False, is_deleted=False,
    ).count()
    if executor_count == 0:
        # An empty pool must never instant-pass ALL (0 >= 0); the task holds
        # here until the pool is refilled. Recovery: re_evaluate on any
        # subsequent (duplicate) vote or the ops management command.
        logger.error(
            "tasks_management.flow: task %s held at step %s (order %s) - executor "
            "pool '%s' is empty; refill the pool to resume",
            _task.id, _step.id, _step.order, _step.task_group.code,
        )
        return None

    approvals = decisions.filter(decision=TaskDecision.Decision.APPROVED).count()
    policy = _step.effective_policy()
    threshold = _step.effective_threshold()
    if policy == TaskFlowStep.StepCompletionPolicy.ALL:
        # >= not ==: pool shrink after votes were cast must not strand the task
        passed = approvals >= executor_count
    elif policy == TaskFlowStep.StepCompletionPolicy.ANY:
        passed = approvals >= 1
    elif policy == TaskFlowStep.StepCompletionPolicy.N:
        if not threshold:
            logger.error(
                "tasks_management.flow: task %s step %s has policy N without a "
                "threshold; holding", _task.id, _step.id,
            )
            return None
        passed = approvals >= threshold
    else:
        logger.error(
            "tasks_management.flow: task %s step %s has unknown policy '%s'; holding",
            _task.id, _step.id, policy,
        )
        return None

    if not passed:
        return None

    return _advance_or_complete(_task, _step, _user)


def _claim_terminal_transition(_task, outcome, _user):
    """
    Write the terminal status while still holding the caller's row lock, so a
    concurrent voter's own locked re-read sees a non-ACCEPTED status and bails
    at this function's existing guard instead of also deciding the step has
    passed. Without this, two votes that each pass the step under their own
    lock (the lock only serializes the read-evaluate-write of one call at a
    time, it does not remember a decision made by a prior holder) would both
    proceed to call complete_task() after releasing the lock, running every
    consumer completion handler twice.

    TaskService.complete_task() still runs the actual completion afterwards,
    outside the lock, exactly once - for whichever call's outcome is non-None,
    since a racer that lost the claim never computes one. The status write
    here is deliberately redundant with what complete_task() does; the point
    is not the value, it is making the transition visible to a racer before
    the lock is released.
    """
    if outcome not in ('completed', 'failed'):
        return
    _task.status = Task.Status.FAILED if outcome == 'failed' else Task.Status.COMPLETED
    _task.save(username=_user.login_name)


def re_evaluate_flow_task(_task_id, _user):
    """
    Ops re-evaluation of a flow task's current step without recording a vote.
    Used by the re_evaluate_task_step management command, e.g. after refilling
    an emptied executor pool, so held tasks resume without a synthetic re-vote.
    """
    outcome = None
    with transaction.atomic():
        _task = Task.objects.select_for_update(of=('self',)).select_related(
            'current_step__task_group', 'current_step', 'flow',
        ).get(id=_task_id)
        if _task.status != Task.Status.ACCEPTED or not _task.flow_id or not _task.current_step_id:
            logger.warning(
                "tasks_management.flow: task %s is not an in-review flow task; "
                "nothing to re-evaluate", _task_id,
            )
            return
        outcome = (_evaluate_batch_step if _is_batch_task(_task) else _evaluate_step)(
            _task, _task.current_step, _user)
        _claim_terminal_transition(_task, outcome, _user)
    if outcome == 'failed':
        TaskService(_user).complete_task({"id": _task_id, 'failed': True})
    elif outcome == 'completed':
        TaskService(_user).complete_task({"id": _task_id})


def resolve_flow_task(_task_id, _user, _verdict):
    """
    Flow branch of task resolution. Vote recording, step evaluation,
    advancement and the terminal status claim (see _claim_terminal_transition)
    run inside one transaction under select_for_update on the task row;
    complete_task's consumer-facing side effects run after the lock is
    released so a slow or failing handler never holds the lock or rolls back
    the vote. ValidationErrors deliberately propagate to the mutation layer.
    """
    outcome = None
    with transaction.atomic():
        # of=('self',): lock only the task row - FOR UPDATE cannot be applied
        # to the nullable side of the outer joins select_related introduces.
        _task = Task.objects.select_for_update(of=('self',)).select_related(
            'current_step__task_group', 'current_step', 'flow',
        ).get(id=_task_id)

        if _task.status != Task.Status.ACCEPTED:
            logger.warning(
                "tasks_management.flow: task %s no longer ACCEPTED (%s); vote ignored",
                _task.id, _task.status,
            )
            return
        step = _task.current_step
        if step is None:
            logger.error(
                "tasks_management.flow: task %s has flow %s but no current_step; "
                "cannot evaluate", _task.id, _task.flow_id,
            )
            return

        _validate_flow_vote(_task, step, _user, _verdict)

        if _is_batch_task(_task):
            if not _record_batch_decisions(_task, step, _user, _verdict):
                logger.warning(
                    "tasks_management.flow: batch vote by user %s on task %s step %s "
                    "added no new records; re-evaluating", _user.id, _task.id, step.id,
                )
            outcome = _evaluate_batch_step(_task, step, _user)
        else:
            # Duplicate votes are detected check-then-insert under the lock (an
            # IntegrityError inside this atomic block would poison the
            # transaction) and still re-run evaluation: a re-vote is the
            # documented backstop that resumes a task held on an emptied pool.
            exists = TaskDecision.objects.filter(
                task=_task, flow_step=step, user=_user,
                record_id__isnull=True, is_deleted=False,
            ).exists()
            if exists:
                logger.warning(
                    "tasks_management.flow: duplicate vote by user %s on task %s step %s; "
                    "re-evaluating", _user.id, _task.id, step.id,
                )
            else:
                TaskDecision(
                    task=_task, flow_step=step, user=_user, decision=_verdict,
                ).save(username=_user.login_name)

            outcome = _evaluate_step(_task, step, _user)

        _claim_terminal_transition(_task, outcome, _user)

    if outcome == 'failed':
        TaskService(_user).complete_task({"id": _task.id, 'failed': True})
    elif outcome == 'completed':
        TaskService(_user).complete_task({"id": _task.id})


def on_task_resolve(**kwargs):
    """
    Generic event for checking the completion_policy of a task. If the task is
    completed or failed, TaskService.complete_task is called with the
    appropriate `failed` flag. Tasks with a flow follow the layered path;
    flat tasks follow the legacy path unchanged. On the legacy path a failed
    completion raises TaskCompletionRefused out of TaskService.resolve_task.
    """
    try:
        result = kwargs.get('result', None)
        if not (result and result['success']
                and result['data']['task']['status'] == Task.Status.ACCEPTED):
            return
        payload_task = result['data']['task']
        if payload_task['executor_action_event'] != TasksManagementConfig.default_executor_event:
            if payload_task.get('flow'):
                logger.error(
                    "tasks_management.flow: flow task %s resolved with non-default "
                    "executor_action_event '%s' - flows only support the default "
                    "event; the task will not advance",
                    payload_task.get('id'), payload_task['executor_action_event'],
                )
            return
        data = kwargs.get("result").get("data")
        task = Task.objects.select_related('task_group').prefetch_related('task_group__taskexecutor_set').get(
            id=data["task"]["id"])
        user = User.objects.get(id=data["user"]["id"])
        # Prefer the request's own verdict, which resolve_task carries
        # separately. business_status is the deep-MERGED history: its lists
        # are concatenated across steps, so a reviewer who sits in two steps'
        # pools would see their step-1 ids replayed against step 2, and an id
        # appearing in both ACCEPT and REJECT collapses to whichever is read
        # first. Fall back to the merged blob only for callers that predate
        # the extra payload key.
        incoming_status = data.get('incoming_status')
        if incoming_status is None:
            incoming_status = payload_task.get('business_status') or {}
        verdict = incoming_status.get(str(user.id))
    except Exception as e:
        logger.error("Error while executing on_task_resolve", exc_info=e)
        return [str(e)]

    if task.flow_id:
        # ValidationErrors must reach journalize/MutationLog - no swallowing.
        return resolve_flow_task(task.id, user, verdict)

    try:
        _record_flat_decisions(task, user, verdict)

        if not task.task_group:
            logger.error("Resolving task not assigned to TaskGroup: %s", data['task']['id'])
            return ['Task not assigned to TaskGroup']

        resolvers = {
            'ALL': resolve_task_all,
            'ANY': resolve_task_any,
            'N': resolve_task_n,
        }

        if task.task_group.completion_policy not in resolvers:
            logger.error("Resolving task with unknown completion_policy: %s", task.task_group.completion_policy)
            return ['Unknown completion_policy: %s' % task.task_group.completion_policy]

        resolvers[task.task_group.completion_policy](task, user)
    except TaskCompletionRefused:
        raise
    except Exception as e:
        logger.error("Error while executing on_task_resolve", exc_info=e)
        return [str(e)]
