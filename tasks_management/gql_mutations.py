import graphene as graphene
from django.db import transaction
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError

from core.gql.gql_mutations.base_mutation import BaseHistoryModelCreateMutationMixin, BaseMutation, \
    BaseHistoryModelUpdateMutationMixin, BaseHistoryModelDeleteMutationMixin
from core.schema import OpenIMISMutation
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import TaskGroup, Task, TaskMutation
from tasks_management.services import TaskGroupService, TaskService, TaskCompletionRefused


class CreateTaskGroupInput(OpenIMISMutation.Input):
    class TaskGroupCompletionPolicyEnum(graphene.Enum):
        ALL = TaskGroup.TaskGroupCompletionPolicy.ALL
        ANY = TaskGroup.TaskGroupCompletionPolicy.ANY
        N = TaskGroup.TaskGroupCompletionPolicy.N

    code = graphene.String(required=True, max_length=255)
    completion_policy = graphene.Field(TaskGroupCompletionPolicyEnum, required=True)
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
                    service.delete({'id': id, 'user': user})

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


class ResolveTaskMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "ResolveTaskMutation"
    _mutation_module = "tasks_management"
    _model = Task

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
