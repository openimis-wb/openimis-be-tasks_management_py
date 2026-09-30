import graphene as graphene
from django.db import transaction
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError

from core.gql.gql_mutations.base_mutation import BaseHistoryModelCreateMutationMixin, BaseMutation, \
    BaseHistoryModelUpdateMutationMixin, BaseHistoryModelDeleteMutationMixin
from core.schema import OpenIMISMutation
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import TaskGroup, Task, TaskMutation, TaskExecutor, TaskFlow, TaskFlowStep
from tasks_management.services import TaskGroupService, TaskService, TaskFlowService, TaskCompletionRefused


class CreateTaskGroupInput(OpenIMISMutation.Input):
    class TaskGroupCompletionPolicyEnum(graphene.Enum):
        ALL = TaskGroup.TaskGroupCompletionPolicy.ALL
        ANY = TaskGroup.TaskGroupCompletionPolicy.ANY
        N = TaskGroup.TaskGroupCompletionPolicy.N

    code = graphene.String(required=True, max_length=255)
    completion_policy = graphene.Field(TaskGroupCompletionPolicyEnum, required=True)
    # Required for N, forbidden otherwise - mirrors TaskFlowStep's own
    # threshold field and validate_task_group's check on it.
    threshold = graphene.Int(required=False)
    user_ids = graphene.List(graphene.UUID)
    task_sources = graphene.List(graphene.String)

    def resolve_completion_policy(self, info):
        return self.completion_policy


class UpdateTaskInput(OpenIMISMutation.Input):
    class TaskStatusEnum(graphene.Enum):
        COMPLETED = Task.Status.COMPLETED
        FAILED = Task.Status.FAILED
        ACCEPTED = Task.Status.ACCEPTED

    id = graphene.UUID(required=True)
    status = graphene.Field(TaskStatusEnum, required=False)
    task_group_id = graphene.UUID(required=False)
    # Assignment is one decision with two shapes: an ordered approval flow or
    # a flat task group. Setting a flow parks the task on step 1 and derives
    # its group from that step, so the two are mutually exclusive. Detaching
    # is an explicit flag, never an omitted or null flow_id.
    flow_id = graphene.UUID(required=False)
    detach_flow = graphene.Boolean(required=False)


class UpdateTaskGroupInput(CreateTaskGroupInput):
    id = graphene.UUID(required=True)


class ResolveTaskGroupInput(OpenIMISMutation.Input):
    id = graphene.UUID(required=True)
    business_status = graphene.JSONString(required=True)
    additional_data = graphene.JSONString(required=False)


class CreateTaskGroupMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "CreateTaskGroupMutation"
    _mutation_module = "tasks_management"
    _model = TaskGroup

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                TasksManagementConfig.gql_task_group_create_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = TaskGroupService(user)
        response = service.create(data)
        if not response['success']:
            return response
        return None

    class Input(CreateTaskGroupInput):
        pass


class UpdateTaskGroupMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "UpdateTaskGroupMutation"
    _mutation_module = "tasks_management"
    _model = TaskGroup

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                TasksManagementConfig.gql_task_group_update_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = TaskGroupService(user)
        response = service.update(data)
        if not response['success']:
            return response
        return None

    class Input(UpdateTaskGroupInput):
        pass


class DeleteTaskGroupMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "DeleteTaskGroupMutation"
    _mutation_module = "tasks_management"
    _model = TaskGroup

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                TasksManagementConfig.gql_task_group_delete_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = TaskGroupService(user)
        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for id in ids:
                    # The service can now refuse a delete (a group used as a
                    # flow step pool); discarding the result would report
                    # GraphQL success while nothing was deleted.
                    response = service.delete({'id': id, 'user': user})
                    if response and not response.get('success'):
                        raise ValidationError(str(response))

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID)


class UpdateTaskMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "UpdateTaskMutation"
    _mutation_module = "tasks_management"
    _model = Task

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                TasksManagementConfig.gql_task_update_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        client_mutation_id = data.pop('client_mutation_id', None)
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = TaskService(user)
        res = service.update(data)
        task = Task.objects.filter(id=data['id']).first()

        if client_mutation_id:
            TaskMutation.object_mutated(
                user, client_mutation_id=client_mutation_id, task=task
            )
        if not res['success']:
            return res
        return None

    class Input(UpdateTaskInput):
        pass


class TaskFlowStepInput(graphene.InputObjectType):
    class StepCompletionPolicyEnum(graphene.Enum):
        ALL = TaskFlowStep.StepCompletionPolicy.ALL
        ANY = TaskFlowStep.StepCompletionPolicy.ANY
        N = TaskFlowStep.StepCompletionPolicy.N

    task_group_id = graphene.UUID(required=True)
    # Omitted/null policy = inherit the pool group's policy and threshold
    completion_policy = graphene.Field(StepCompletionPolicyEnum, required=False)
    threshold = graphene.Int(required=False)


class CreateTaskFlowInput(OpenIMISMutation.Input):
    code = graphene.String(required=True, max_length=255)
    name = graphene.String(required=False, max_length=255)
    task_sources = graphene.List(graphene.String)
    steps = graphene.List(TaskFlowStepInput, required=True)


class UpdateTaskFlowInput(CreateTaskFlowInput):
    # Head-level, non-semantic changes: name and source binding (affects new
    # tasks only). A modified steps payload is refused by the service - step
    # changes go through replaceTaskFlow.
    id = graphene.UUID(required=True)
    steps = graphene.List(TaskFlowStepInput, required=False)


class ReplaceTaskFlowInput(OpenIMISMutation.Input):
    id = graphene.UUID(required=True)
    code = graphene.String(required=False, max_length=255)
    name = graphene.String(required=False, max_length=255)
    task_sources = graphene.List(graphene.String)
    steps = graphene.List(TaskFlowStepInput, required=False)


def _steps_payload(data):
    steps = data.pop('steps', None)
    if steps is None:
        return data, None
    normalized = [
        {
            'task_group_id': str(step['task_group_id']),
            'completion_policy': step.get('completion_policy'),
            'threshold': step.get('threshold'),
        }
        for step in steps
    ]
    return data, normalized


class CreateTaskFlowMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "CreateTaskFlowMutation"
    _mutation_module = "tasks_management"
    _model = TaskFlow

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                TasksManagementConfig.gql_task_flow_create_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        data, steps = _steps_payload(data)
        response = TaskFlowService(user).create({**data, 'steps': steps or []})
        if not response['success']:
            return response
        return None

    class Input(CreateTaskFlowInput):
        pass


class UpdateTaskFlowMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "UpdateTaskFlowMutation"
    _mutation_module = "tasks_management"
    _model = TaskFlow

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                TasksManagementConfig.gql_task_flow_update_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        data, steps = _steps_payload(data)
        if steps is not None:
            data['steps'] = steps
        response = TaskFlowService(user).update(data)
        if not response['success']:
            return response
        return None

    class Input(UpdateTaskFlowInput):
        pass


class ReplaceTaskFlowMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "ReplaceTaskFlowMutation"
    _mutation_module = "tasks_management"
    _model = TaskFlow

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                TasksManagementConfig.gql_task_flow_update_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        data, steps = _steps_payload(data)
        if steps is not None:
            data['steps'] = steps
        response = TaskFlowService(user).replace(data)
        if not response['success']:
            return response
        return None

    class Input(ReplaceTaskFlowInput):
        pass


class DeleteTaskFlowMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "DeleteTaskFlowMutation"
    _mutation_module = "tasks_management"
    _model = TaskFlow

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                TasksManagementConfig.gql_task_flow_delete_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)
        service = TaskFlowService(user)
        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for id in ids:
                    response = service.delete({'id': id})
                    if not response['success']:
                        raise ValidationError(str(response))

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID)


class ResolveTaskMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "ResolveTaskMutation"
    _mutation_module = "tasks_management"
    _model = Task

    @classmethod
    def _validate_mutation(cls, user, **data):
        # A resolver must be an executor of the task's current group or hold
        # triage/admin rights - mirrors the task visibility rule. Before this
        # check any authenticated user could submit arbitrary business_status
        # payloads (see the flat-path hardening note in the module README).
        if type(user) is AnonymousUser or not user.id:
            raise ValidationError("mutation.authentication_required")
        task = Task.objects.filter(id=data.get('id')).first()
        if not task:
            return
        from tasks_management.gql_queries import is_task_triage
        if getattr(user, 'is_imis_admin', False) or is_task_triage(user):
            return
        is_executor = task.task_group_id and TaskExecutor.objects.filter(
            task_group_id=task.task_group_id, user_id=user.id, is_deleted=False,
        ).exists()
        if not is_executor:
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        client_mutation_id = data.pop('client_mutation_id', None)
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = TaskService(user)
        try:
            res = service.resolve_task(data)
        except TaskCompletionRefused as refusal:
            res = refusal.result
        task = Task.objects.filter(id=data['id']).first()

        if client_mutation_id:
            TaskMutation.object_mutated(
                user, client_mutation_id=client_mutation_id, task=task
            )
        if not res['success']:
            return res
        return None

    class Input(ResolveTaskGroupInput):
        pass
