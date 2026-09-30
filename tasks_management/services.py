import copy
import datetime
import decimal
import logging
import uuid
from abc import abstractmethod, ABC
from typing import Dict, Type
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.core.exceptions import ValidationError

from core.datetimes.ad_datetime import AdDate, AdDatetime
from core.forms import User
from core.models import HistoryModel
from core.services import BaseService
from core.signals import register_service_signal
from core.services.utils import check_authentication, output_exception, output_result_success, model_representation
from core.utils import to_json_safe_value
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import TaskGroup, TaskExecutor, Task, TaskFlow, TaskFlowStep
from tasks_management.validation import TaskGroupValidation, TaskExecutorValidation, TaskValidation, \
    TaskFlowValidation, validate_task_flow_assignment

logger = logging.getLogger(__name__)


class TaskCompletionRefused(Exception):
    """
    Raised through TaskService.resolve_task when the completion that the resolution triggers fails.
    `result` is the failed TaskService.complete_task output. resolve_task runs in one transaction,
    so the refusal also rolls back the business_status and additional data it stored.
    """

    def __init__(self, result):
        self.result = result
        super().__init__(result.get('detail') or result.get('message'))


class TaskService(BaseService):
    OBJECT_TYPE = Task

    def __init__(self, user, validation_class=TaskValidation):
        super().__init__(user, validation_class)

    @transaction.atomic
    @register_service_signal('task_service.create')
    def create(self, obj_data):
        source = obj_data.get('source')
        flow = self._match_flow_for_source(source, obj_data)
        if flow:
            first_step = flow.steps.filter(is_deleted=False).order_by('order').first()
            if first_step:
                logger.info(
                    "tasks_management.flow: assigning task from source '%s' to flow "
                    "'%s' (version %s), step 1 pool '%s'",
                    source, flow.code, flow.id, first_step.task_group.code,
                )
                obj_data = {
                    **obj_data,
                    "flow": flow,
                    "current_step": first_step,
                    "task_group": first_step.task_group,
                    "status": Task.Status.ACCEPTED,
                }
                return super().create(obj_data)
            logger.error(
                "tasks_management.flow: flow '%s' matched source '%s' but has no "
                "steps; falling back to group binding", flow.code, source,
            )
        task_group_query = TaskGroup.objects.filter(json_ext__contains={"task_sources": [source]})
        if task_group_query:
            obj_data = {**obj_data, "task_group": task_group_query.first(), "status": Task.Status.ACCEPTED}
        return super().create(obj_data)

    def _match_flow_for_source(self, source, obj_data):
        if not source or source in (TasksManagementConfig.flow_ineligible_sources or []):
            return None
        executor_event = obj_data.get('executor_action_event')
        if executor_event != TasksManagementConfig.default_executor_event:
            # Flows only advance through the generic resolver; tasks with a
            # custom executor event would assign a flow and then never move.
            flow_bound = TaskFlow.objects.filter(
                json_ext__contains={"task_sources": [source]},
                is_deleted=False, replacement_uuid__isnull=True,
            ).exists()
            if flow_bound:
                logger.error(
                    "tasks_management.flow: source '%s' is flow-bound but the task "
                    "carries executor_action_event '%s' (not the default); ignoring "
                    "the flow binding", source, executor_event,
                )
            return None
        # Head versions only - superseded versions keep serving their pinned
        # in-flight tasks but never receive new ones.
        return TaskFlow.objects.filter(
            json_ext__contains={"task_sources": [source]},
            is_deleted=False, replacement_uuid__isnull=True,
        ).first()

    @register_service_signal('task_service.update')
    def update(self, obj_data):
        obj_data, assignment = self._pop_flow_assignment(obj_data)
        if assignment is not None:
            # Validating the assignment and persisting it must be one unit:
            # otherwise a vote can land between "no decisions yet" and the
            # save, and the stale assignment still wins - moving a task that
            # has already been voted on to a different flow.
            with transaction.atomic():
                task = self.OBJECT_TYPE.objects.select_for_update(of=('self',)).filter(
                    id=obj_data.get('id')).first()
                return super().update(self._apply_flow_assignment(task, obj_data, assignment))
        task = self.OBJECT_TYPE.objects.filter(id=obj_data.get('id')).first()
        if task and task.flow_id:
            incoming_group = obj_data.get('task_group_id', obj_data.get('task_group'))
            incoming_group_id = getattr(incoming_group, 'id', incoming_group)
            if incoming_group_id and str(incoming_group_id) != str(task.task_group_id):
                raise ValidationError(
                    "tasks_management.flow: task '%s' belongs to flow '%s' - its "
                    "group is managed by step advancement and cannot be reassigned"
                    % (task.id, task.flow.code)
                )
            incoming_status = obj_data.get('status')
            if incoming_status in (Task.Status.COMPLETED, Task.Status.FAILED):
                # A privileged resolver votes through resolve_task like anyone
                # else - complete_task() is the only legitimate way to close a
                # task, and it is reached through step evaluation, not here.
                # Without this, task-update rights alone (no flow membership,
                # no vote, no ledger entry) would force-close a task mid-review.
                raise ValidationError(
                    "tasks_management.flow: task '%s' belongs to flow '%s' - it can "
                    "only be closed by completing its current step, not by setting "
                    "status directly" % (task.id, task.flow.code)
                )
        return super().update(obj_data)

    def _pop_flow_assignment(self, obj_data):
        """
        Split the flow assignment out of the update payload.

        Detaching is an explicit flag rather than a null flow_id so an update
        that simply does not mention the flow - a status change, a group edit -
        can never silently unbind a review that is already running.

        Returns (payload, assignment) where assignment is None for "not
        requested", False for "detach" or a flow id to attach.
        """
        obj_data = dict(obj_data)
        flow_id = obj_data.pop('flow_id', None)
        detach = obj_data.pop('detach_flow', False)
        if flow_id and detach:
            raise ValidationError(
                "tasks_management.flow: flowId and detachFlow are mutually "
                "exclusive - a caller cannot attach and detach in the same "
                "update"
            )
        if flow_id:
            return obj_data, flow_id
        if detach:
            return obj_data, False
        return obj_data, None

    def _apply_flow_assignment(self, task, obj_data, assignment):
        """
        Attaching mirrors what create() does for a source-bound task: pin the
        flow, park the task on step 1 and hand it to that step's pool. The two
        shapes of assignment are mutually exclusive, so any group coming in on
        the same payload is dropped - a flow task's group is derived.
        """
        detach = assignment is False
        flow = None if detach else TaskFlow.objects.filter(id=assignment).first()
        errors = validate_task_flow_assignment(task, flow, detach=detach)
        if errors:
            raise ValidationError(errors)

        if detach:
            logger.info(
                "tasks_management.flow: task %s detached from flow '%s' - it keeps "
                "task group '%s' and resolves flat from here",
                task.id, task.flow.code,
                task.task_group.code if task.task_group else None,
            )
            return {**obj_data, "flow": None, "current_step": None}

        first_step = flow.steps.filter(is_deleted=False).order_by('order').first()
        logger.info(
            "tasks_management.flow: task %s assigned to flow '%s' (version %s), "
            "step 1 pool '%s'",
            task.id, flow.code, flow.id, first_step.task_group.code,
        )
        obj_data.pop('task_group_id', None)
        obj_data.pop('task_group', None)
        return {
            **obj_data,
            "flow": flow,
            "current_step": first_step,
            "task_group": first_step.task_group,
            "status": Task.Status.ACCEPTED,
        }

    @register_service_signal('task_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)

    @register_service_signal('task_service.complete_task')
    def complete_task(self, obj_data):
        try:
            with transaction.atomic():
                obj = self.OBJECT_TYPE.objects.get(id=obj_data['id'])
                obj.status = Task.Status.FAILED if obj_data.get('failed', False) else Task.Status.COMPLETED
                obj.save(username=self.user.login_name)
                return output_result_success({'task': model_representation(obj), 'user': {'id': f"{self.user.id}"}})
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="complete", exception=exc)

    # AFTER-signal handlers (this module's own flow branch included) can raise
    # ValidationError once vote recording/evaluation is underway; without this
    # wrapper the business_status merge below has already committed by the
    # time that happens, so the mutation reports failure while the invalid
    # vote is left persisted. @transaction.atomic makes the merge and every
    # AFTER handler one unit, matching create()'s existing pattern.
    @transaction.atomic
    @register_service_signal('task_service.resolve_task')
    def resolve_task(self, obj_data):
        try:
            self.validation_class.validate_update(self.user, **obj_data)
            obj = self.OBJECT_TYPE.objects.get(id=obj_data['id'])
            incoming_status = obj_data.get('business_status')
            additional_data = obj_data.get('additional_data')
            self._update_task_business_status(obj, incoming_status, additional_data)
            self._insert_additional_data_to_json_ext(obj, additional_data)
            # incoming_status is THIS request's verdict, carried separately
            # from the task's merged business_status. __deep_merge below
            # concatenates lists, so for a per-record vote the merged blob
            # accumulates every step's ids - a reviewer sitting in two steps'
            # pools would have their earlier verdict replayed against the
            # later step, and an id present in both lists resolves to the
            # first one seen. Handlers must read the verdict from here.
            return output_result_success({
                'task': model_representation(obj),
                'user': {'id': f"{self.user.id}"},
                'incoming_status': incoming_status,
            })
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="resolve", exception=exc)

    def _update_task_business_status(self, task, incoming_status, additional_data):
        task.business_status = self.__deep_merge(task.business_status, incoming_status)
        self._insert_additional_data_to_json_ext(task, additional_data)
        task.save(username=self.user.login_name)

    def _insert_additional_data_to_json_ext(self, obj, additional_data):
        if not additional_data:
            return

        obj.json_ext = obj.json_ext or {}

        existing_additional_data = obj.json_ext.get("additional_resolve_data", {})
        existing_additional_data[str(self.user.id)] = additional_data

        obj.json_ext["additional_resolve_data"] = existing_additional_data

    def __deep_merge(self, dict1, dict2):
        """
        Merges two dictionaries, deeply combining them.
        """
        result = copy.deepcopy(dict1)

        for key, value in dict2.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self.__deep_merge(result[key], value)
            elif key in result and isinstance(result[key], list) and isinstance(value, list):
                result[key] = result[key] + value
            else:
                result[key] = copy.deepcopy(value)

        return result


class TaskGroupService(BaseService):
    OBJECT_TYPE: Type[TaskGroup] = TaskGroup

    def __init__(self, user, validation_class=TaskGroupValidation):
        super().__init__(user, validation_class)

    @check_authentication
    def create(self, obj_data: Dict[str, any]):
        try:
            with transaction.atomic():
                user_ids = obj_data.pop('user_ids')
                obj_data = self._adjust_update_payload(obj_data)
                self.validation_class.validate_create(self.user, **obj_data)
                task_sources = obj_data.pop('task_sources')
                obj_data = {**obj_data, "json_ext": {"task_sources": list(task_sources)}}
                obj_: TaskGroup = self.OBJECT_TYPE(**obj_data)
                task_group_output = self.save_instance(obj_)
                task_group_id = task_group_output['data']['id']
                task_executor_service = TaskExecutorService(self.user)
                # TODO: it would be good to override bulk_create and use it here
                for user_id in user_ids:
                    task_executor_service.create({
                        "task_group_id": task_group_id,
                        "user_id": user_id
                    })
                return task_group_output
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

    @check_authentication
    def update(self, obj_data: Dict[str, any]):
        try:
            with transaction.atomic():
                user_ids = obj_data.pop('user_ids')
                obj_data = self._adjust_update_payload(obj_data)
                self.validation_class.validate_update(self.user, **obj_data)
                task_sources = obj_data.pop('task_sources')
                task_group_id = obj_data.get('id')
                task_group = TaskGroup.objects.get(id=task_group_id)
                json_ext = task_group.json_ext if task_group.json_ext else dict()
                obj_data = {**obj_data, "json_ext": {**json_ext, "task_sources": list(task_sources)}}
                current_task_executors = task_group.taskexecutor_set.filter(is_deleted=False)
                current_user_ids = current_task_executors.values_list('user__id', flat=True)
                if set(current_user_ids) != set(user_ids):
                    self._update_task_group_task_executors(task_group, user_ids)
                return super().update(obj_data)
        except Exception as exc:
            import traceback
            logger.debug(traceback.format_exc())
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

    @transaction.atomic
    def _update_task_group_task_executors(self, task_group, user_ids):
        try:
            task_group.taskexecutor_set.all().delete()
            service = TaskExecutorService(self.user)
            for user_id in user_ids:
                service.create({'task_group_id': task_group.id,
                                'user_id': user_id})
            if not user_ids and TaskFlowStep.objects.filter(
                task_group=task_group, is_deleted=False,
                flow__is_deleted=False, flow__replacement_uuid__isnull=True,
            ).exists():
                logger.warning(
                    "tasks_management.flow: task group '%s' is a flow step pool and "
                    "now has no executors - flow tasks reaching that step will hold "
                    "until the pool is refilled", task_group.code,
                )
        except Exception as exc:
            raise exc

    def delete(self, obj_data: Dict[str, any]):
        id = obj_data.get("id")
        if id:
            head_step_ref = TaskFlowStep.objects.filter(
                task_group_id=id, is_deleted=False,
                flow__is_deleted=False, flow__replacement_uuid__isnull=True,
            ).select_related('flow').first()
            # Not just the task's current step: an in-flight task's pinned
            # flow version can have a *later* step using this group too - it
            # has not reached that step yet, so current_step would not catch
            # it, but deleting the group now would still hand the task an
            # executor-less pool once it advances. Join through the task's
            # own pinned flow's live steps rather than its current position.
            in_flight_ref = Task.objects.filter(
                flow__steps__task_group_id=id, flow__steps__is_deleted=False,
                is_deleted=False,
                status__in=[Task.Status.RECEIVED, Task.Status.ACCEPTED],
            ).exists()
            if head_step_ref or in_flight_ref:
                flow_code = head_step_ref.flow.code if head_step_ref else 'a superseded version with in-flight tasks'
                return output_exception(
                    model_name=self.OBJECT_TYPE.__name__, method="delete",
                    exception=ValidationError(
                        "tasks_management.flow: task group is used as a step pool by "
                        "flow '%s' - remove the step or replace the flow first" % flow_code
                    ),
                )
            task_group = TaskGroup.objects.filter(id=id).first()
            task_group.taskexecutor_set.all().delete()
        return super().delete(obj_data)

    def _base_payload_adjust(self, obj_data):
        task_sources = obj_data.pop('task_sources', [])
        if task_sources:
            task_sources = set(task_sources)
        return {**obj_data, 'task_sources': task_sources}


class TaskExecutorService(BaseService):
    OBJECT_TYPE: Type[TaskExecutor] = TaskExecutor

    def __init__(self, user, validation_class=TaskExecutorValidation):
        super().__init__(user, validation_class)


class TaskFlowService(BaseService):
    OBJECT_TYPE: Type[TaskFlow] = TaskFlow

    def __init__(self, user, validation_class=TaskFlowValidation):
        super().__init__(user, validation_class)

    @check_authentication
    def create(self, obj_data: Dict[str, any]):
        try:
            with transaction.atomic():
                steps = obj_data.pop('steps', None) or []
                task_sources = obj_data.pop('task_sources', None) or []
                self.validation_class.validate_create(
                    self.user, **obj_data, steps=steps, task_sources=task_sources)
                obj_data = {**obj_data, "json_ext": {"task_sources": list(task_sources)}}
                flow = self.OBJECT_TYPE(**obj_data)
                output = self.save_instance(flow)
                self._create_steps(flow, steps)
                return output
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

    @check_authentication
    def update(self, obj_data: Dict[str, any]):
        """
        Head-level, non-semantic changes only: name and source binding (which
        affects new tasks exclusively). A modified steps payload is refused -
        step changes are semantic and must go through replace() so in-flight
        tasks keep their pinned version.
        """
        try:
            with transaction.atomic():
                steps = obj_data.pop('steps', None)
                task_sources = obj_data.pop('task_sources', None)
                flow = self.OBJECT_TYPE.objects.get(id=obj_data['id'])
                if flow.replacement_uuid:
                    raise ValidationError(
                        "tasks_management.flow: this flow version is superseded and "
                        "cannot be updated")
                if steps is not None and self._steps_differ(flow, steps):
                    raise ValidationError(
                        "tasks_management.flow: steps changed - use replaceTaskFlow "
                        "to create a new version; update only covers name and "
                        "source binding")
                if task_sources is None:
                    task_sources = (flow.json_ext or {}).get('task_sources', [])
                self.validation_class.validate_update(
                    self.user, **obj_data, task_sources=task_sources)
                json_ext = flow.json_ext if flow.json_ext else dict()
                obj_data = {**obj_data, "json_ext": {**json_ext, "task_sources": list(task_sources)}}
                return super().update(obj_data)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

    @check_authentication
    def replace(self, obj_data: Dict[str, any]):
        """
        Semantic edit: creates a new head version and supersedes this one.
        Deliberately NOT core's replace_object(): that saves the new head
        before superseding the old row, transiently violating the head-scoped
        unique_task_flow_code constraint. Here the old head is superseded
        FIRST, then the new head is low-level inserted with a pre-generated
        pk, then the steps are re-created against it - one transaction.
        In-flight tasks keep following their pinned version.
        """
        try:
            with transaction.atomic():
                old_flow = self.OBJECT_TYPE.objects.get(id=obj_data['id'])
                if old_flow.replacement_uuid:
                    raise ValidationError(
                        "tasks_management.flow: this flow version is already superseded")
                steps = obj_data.pop('steps', None)
                if steps is None:
                    steps = self._current_step_payloads(old_flow)
                task_sources = obj_data.pop('task_sources', None)
                if task_sources is None:
                    task_sources = (old_flow.json_ext or {}).get('task_sources', [])
                code = obj_data.get('code') or old_flow.code
                name = obj_data.get('name', old_flow.name)
                self.validation_class.validate_replace(
                    self.user, id=str(old_flow.id), code=code, steps=steps,
                    task_sources=task_sources)

                now = datetime.datetime.now()
                new_id = uuid.uuid4()
                # Continue the lineage counter: the visible "version" keeps
                # incrementing across replaces instead of resetting to 1 on
                # every new head row. Captured before the supersede save below
                # bumps the old row's own counter.
                new_version = old_flow.version + 1
                old_flow.replacement_uuid = new_id
                old_flow.date_valid_to = now
                old_flow.save(username=self.user.login_name)

                new_flow = self.OBJECT_TYPE(
                    id=new_id, code=code, name=name,
                    json_ext={"task_sources": list(task_sources)},
                    date_valid_from=now,
                    date_created=now, date_updated=now,
                    user_created=self.user, user_updated=self.user,
                    version=new_version,
                )
                # HistoryModel.save treats a preset pk as an update; insert at
                # the plain-Model level (simple-history still records via its
                # post_save receiver).
                super(HistoryModel, new_flow).save(force_insert=True)
                self._create_steps(new_flow, steps)
                logger.info(
                    "tasks_management.flow: flow '%s' replaced - version %s "
                    "superseded by %s", code, old_flow.id, new_id,
                )
                return {
                    "success": True,
                    "message": "Ok",
                    "detail": "",
                    "old_object": str(old_flow.id),
                    "uuid_new_object": str(new_id),
                }
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="replace", exception=exc)

    def delete(self, obj_data: Dict[str, any]):
        try:
            with transaction.atomic():
                flow = self.OBJECT_TYPE.objects.get(id=obj_data['id'])
                lineage = self._lineage(flow)
                in_flight = Task.objects.filter(
                    flow__in=lineage, is_deleted=False,
                    status__in=[Task.Status.RECEIVED, Task.Status.ACCEPTED],
                ).count()
                if in_flight:
                    raise ValidationError(
                        "tasks_management.flow: flow '%s' (or a version it "
                        "superseded) has %s task(s) still in review - resolve "
                        "them before deleting the flow"
                        % (flow.code, in_flight))
                sources = (flow.json_ext or {}).get('task_sources', [])
                if sources and not flow.replacement_uuid:
                    logger.warning(
                        "tasks_management.flow: deleting flow '%s' leaves source(s) "
                        "%s unbound - new tasks from them will sit in RECEIVED with "
                        "no task group; rebind them to a group or another flow",
                        flow.code, sources,
                    )
                for step in flow.steps.filter(is_deleted=False):
                    step.delete(username=self.user.login_name)
                self._retire_superseded_versions(flow)
                return super().delete(obj_data)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="delete", exception=exc)

    def _lineage(self, flow):
        """
        Every version behind `flow`, walking replacement_uuid back to the
        original. A head being deleted also retires its whole predecessor
        chain (see _retire_superseded_versions), so the in-flight check has
        to cover that same set - a task pinned to a superseded predecessor is
        still "in review" for the purpose of blocking this delete, even
        though it is invisible if the caller only looks at the head.
        """
        lineage = [flow.id]
        seen = {flow.id}
        current = flow
        while True:
            predecessor = self.OBJECT_TYPE.objects.filter(
                replacement_uuid=current.id, is_deleted=False,
            ).first()
            if not predecessor or predecessor.id in seen:
                return lineage
            seen.add(predecessor.id)
            lineage.append(predecessor.id)
            current = predecessor

    def _retire_superseded_versions(self, flow):
        """
        Soft-delete the whole superseded lineage behind `flow`.

        Deleting a flow retires its code, not just the current version.
        Without this, core's HistoryModel.delete() clears replacement_uuid on
        the row pointing at the deleted one ("so a new replacement could be
        generated") - which would promote the previous version back to head:
        new tasks would silently start routing through an old definition, and
        the head-scoped unique_task_flow_code index would be violated while
        both rows are momentarily live. Retiring predecessors first keeps
        them excluded from that index (it covers non-deleted rows only), so
        core's un-linking becomes a no-op on already-retired rows.
        """
        seen = set()
        current = flow
        while True:
            predecessor = self.OBJECT_TYPE.objects.filter(
                replacement_uuid=current.id, is_deleted=False,
            ).first()
            if not predecessor or predecessor.id in seen:
                return
            seen.add(predecessor.id)
            predecessor.is_deleted = True
            # HistoryModel.save refuses to update a replaced row ("you cannot
            # update replaced entity"), so retire it at the plain-Model level
            # exactly as replace() inserts the new head (simple-history still
            # records it through its post_save receiver).
            super(HistoryModel, predecessor).save(update_fields=['is_deleted'])
            current = predecessor

    def _create_steps(self, flow, steps):
        for position, step_data in enumerate(steps, start=1):
            step = TaskFlowStep(
                flow=flow,
                task_group_id=step_data['task_group_id'],
                order=position,
                completion_policy=step_data.get('completion_policy'),
                threshold=step_data.get('threshold'),
            )
            step.save(username=self.user.login_name)

    @staticmethod
    def _current_step_payloads(flow):
        return [
            {
                'task_group_id': step.task_group_id,
                'completion_policy': step.completion_policy,
                'threshold': step.threshold,
            }
            for step in flow.steps.filter(is_deleted=False).order_by('order')
        ]

    @classmethod
    def _steps_differ(cls, flow, steps):
        current = [
            (str(s['task_group_id']), s['completion_policy'], s['threshold'])
            for s in cls._current_step_payloads(flow)
        ]
        incoming = [
            (str(s.get('task_group_id')), s.get('completion_policy'), s.get('threshold'))
            for s in steps
        ]
        return current != incoming


class CreateCheckerLogicServiceMixin(ABC):
    """
    Provides default implementation for creating a create task for maker-checker logic.
    To be used in implementations of core.services.BaseService.
    """

    @property
    @abstractmethod
    def OBJECT_TYPE(self):
        pass

    def create_create_task(self, obj_data):
        try:
            with transaction.atomic():
                self.validation_class.validate_create(self.user, **obj_data)
                task_service = TaskService(self.user)
                task_data = {
                    'source': self._create_source,
                    'business_data_serializer': self._get_business_data_serializer(),
                    'executor_action_event': self._create_executor_event,
                    'business_event': self._create_business_event,
                    'data': self._adjust_create_task_data(None, copy.deepcopy(obj_data)),
                }
                return task_service.create(task_data)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create_create_task", exception=exc)

    @property
    def _create_source(self):
        return self.__class__.__name__

    @property
    def _create_business_event(self):
        return f'{self.__class__.__name__}.create'

    @property
    def _create_executor_event(self):
        return TasksManagementConfig.default_executor_event

    def _adjust_create_task_data(self, entity, obj_data):
        return _get_std_crud_task_data_payload(entity, obj_data)

    def _get_business_data_serializer(self):
        return f'{self.__class__.__module__}.{self.__class__.__name__}._business_data_serializer'

    def _business_data_serializer(self, data):
        return data


class UpdateCheckerLogicServiceMixin(ABC):
    """
    Provides default implementation for creating an update task for maker-checker logic.
    To be used in implementations of core.services.BaseService.
    """

    @property
    @abstractmethod
    def OBJECT_TYPE(self):
        pass

    def create_update_task(self, obj_data):
        try:
            with transaction.atomic():
                self.validation_class.validate_update(self.user, **obj_data)
                task_service = TaskService(self.user)
                obj = self.OBJECT_TYPE.objects.get(id=obj_data['id'])
                task_data = {
                    'source': self._update_source,
                    'business_data_serializer': self._get_business_data_serializer(),
                    'entity_id': obj.id,
                    'entity_type': ContentType.objects.get_for_model(self.OBJECT_TYPE),
                    'executor_action_event': self._update_executor_event,
                    'business_event': self._update_business_event,
                    'data': self._adjust_update_task_data(obj, copy.deepcopy(obj_data)),
                }
                return task_service.create(task_data)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create_update_task", exception=exc)

    @property
    def _update_source(self):
        return self.__class__.__name__

    @property
    def _update_business_event(self):
        return f'{self.__class__.__name__}.update'

    @property
    def _update_executor_event(self):
        return TasksManagementConfig.default_executor_event

    def _adjust_update_task_data(self, entity, obj_data):
        self._align_json_ext(obj_data)
        return _get_std_crud_task_data_payload(entity, obj_data)

    def _align_json_ext(self, obj_data):
        json_ext = obj_data.get('json_ext')
        if isinstance(json_ext, dict):
            for key, value in obj_data.items():
                if key in json_ext and json_ext[key] != value:
                    json_ext[key] = to_json_safe_value(value)

    def _get_business_data_serializer(self):
        return f'{self.__class__.__module__}.{self.__class__.__name__}._business_data_serializer'

    def _business_data_serializer(self, data):
        return data


class DeleteCheckerLogicServiceMixin(ABC):
    """
    Provides default implementation for creating a delete task for maker-checker logic.
    To be used in implementations of core.services.BaseService.
    """

    @property
    @abstractmethod
    def OBJECT_TYPE(self):
        pass

    def create_delete_task(self, obj_data):
        try:
            with transaction.atomic():
                self.validation_class.validate_delete(self.user, **obj_data)
                task_service = TaskService(self.user)
                obj = self.OBJECT_TYPE.objects.get(id=obj_data['id'])
                task_data = {
                    'source': self._delete_source,
                    'business_data_serializer': self._get_business_data_serializer(),
                    'entity_id': obj.id,
                    'entity_type': ContentType.objects.get_for_model(self.OBJECT_TYPE),
                    'executor_action_event': self._delete_executor_event,
                    'business_event': self._delete_business_event,
                    'data': self._adjust_delete_task_data(None, copy.deepcopy(obj_data)),
                }
                return task_service.create(task_data)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create_delete_task", exception=exc)

    @property
    def _delete_source(self):
        return self.__class__.__name__

    @property
    def _delete_business_event(self):
        return f'{self.__class__.__name__}.delete'

    @property
    def _delete_executor_event(self):
        return TasksManagementConfig.default_executor_event

    def _adjust_delete_task_data(self, entity, obj_data):
        return _get_std_crud_task_data_payload(entity, obj_data)

    def _get_business_data_serializer(self):
        return f'{self.__class__.__module__}.{self.__class__.__name__}._business_data_serializer'

    def _business_data_serializer(self, data):
        return data


class CheckerLogicServiceMixin(CreateCheckerLogicServiceMixin,
                               UpdateCheckerLogicServiceMixin,
                               DeleteCheckerLogicServiceMixin,
                               ABC):
    """
    Provides default implementation for creating "create", "update", and "delete" tasks for maker-checker logic
    To be used in implementations of core.services.BaseService.
    """
    pass


def on_task_complete_service_handler(service_type):
    """
    Generic complete_task handler any combination of CreateCheckerLogicServiceMixin,
    UpdateCheckerLogicServiceMixin, DeleteCheckerLogicServiceMixin. It will automatically detect available
    task business events fot that service type.

    :param service_type: BaseService subclass implementing any <Operation>CheckerLogicServiceMixin
    :return: event handler that will be able to execute task
    """
    operations = []
    if issubclass(service_type, CreateCheckerLogicServiceMixin):
        operations.append('create')
    if issubclass(service_type, UpdateCheckerLogicServiceMixin):
        operations.append('update')
    if issubclass(service_type, DeleteCheckerLogicServiceMixin):
        operations.append('delete')

    def service_operation_handler(operation, user, data):
        # Run the operation form a service by name
        # getattr(ExampleService(user), 'update')(data)
        return getattr(service_type(user), operation)(data)

    def func(**kwargs):
        try:
            result = kwargs.get('result', {})
            task = result['data']['task']
            business_event = task['business_event']
            # Tasks generated with CheckerLogicServiceMixin use naming scheme `ServiceName.operation` as business event.
            # Checking if the task was generated by the mixin and if the service provided for the handler match
            service_match = business_event.startswith(f"{service_type.__name__}.")
            if result and result['success'] \
                    and task['status'] == Task.Status.COMPLETED \
                    and service_match:
                # Extracting `operation` part from `ServiceName.operation`
                operation = business_event.split(".")[1]
                if operation in operations:
                    user = User.objects.get(id=result['data']['user']['id'])
                    data = task['data']['incoming_data']
                    service_operation_handler(operation, user, data)
        except Exception as e:
            logger.error("Error while executing on_task_complete", exc_info=e)
            return [str(e)]

    return func


def _get_std_crud_task_data_payload(entity, payload):
    incoming_data = {}
    current_data = {}

    for key in payload:
        incoming_value = to_json_safe_value(payload[key])
        incoming_data[key] = incoming_value

        if entity:
            entity_value = getattr(entity, key)
            current_data[key] = to_json_safe_value(entity_value) if entity_value else entity_value

    return {"incoming_data": incoming_data, "current_data": current_data}


def _get_std_task_data_payload(payload):
    incoming_data = {}

    for key in payload:
        incoming_value = to_json_safe_value(payload[key])
        incoming_data[key] = incoming_value

    return incoming_data


def crud_business_data_builder(data, serializer):
    serialized_data = copy.deepcopy(data)
    for data_key, data_value in data.items():
        serialized_data[data_key] = {
            key: serializer(key, value) for key, value in data_value.items()
        }
    return serialized_data
