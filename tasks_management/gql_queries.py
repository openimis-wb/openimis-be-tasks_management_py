import importlib

import graphene
import copy

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Q
from graphene_django import DjangoObjectType

from core import ExtendedConnection, prefix_filterset
from core.gql_queries import UserGQLType
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import TaskGroup, TaskExecutor, Task, TaskFlow, TaskFlowStep, TaskDecision

DICT_STRING = "{}"


def entity_string_of(entity_type_id, entity_id):
    """
    Text of the entity a task points to, from its entity_type/entity_id columns. Task history rows
    hold these columns but not the generic relation. None when there is no entity or its row is gone.
    """
    if entity_type_id is None or entity_id is None:
        return None
    try:
        model = ContentType.objects.get_for_id(entity_type_id).model_class()
        entity = model._base_manager.filter(pk=entity_id).first() if model else None
    except (ObjectDoesNotExist, ValueError, TypeError, ValidationError):
        return None
    return str(entity) if entity is not None else None


def is_task_triage(user):
    return user.has_perms(TasksManagementConfig.gql_task_group_create_perms
                          + TasksManagementConfig.gql_task_group_search_perms
                          + TasksManagementConfig.gql_task_group_update_perms
                          + TasksManagementConfig.gql_task_group_delete_perms)


class TaskGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    business_data = graphene.JSONString()
    entity_string = graphene.String()
    decision_count = graphene.Int()

    class Meta:
        model = Task
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "entity_type": ["exact"],
            "entity_id": ["exact"],

            "source": ["exact", "iexact", "istartswith", "icontains"],
            "status": ["exact", "iexact", "istartswith", "icontains"],
            "executor_action_event": ["exact", "iexact", "istartswith", "icontains"],
            "business_event": ["exact", "iexact", "istartswith", "icontains"],

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_decision_count(self, info):
        # Lets the client lock the assignment picker instead of offering a
        # change the service would refuse: votes are recorded against the
        # steps of the flow the task is on.
        return self.decisions.filter(is_deleted=False).count()

    def resolve_business_data(self, info):
        data = self.data
        serializer_path = self.business_data_serializer
        serialized_data = copy.deepcopy(data)
        if serializer_path:
            module_path, class_name, method_name = serializer_path.rsplit('.', 2)

            try:
                service_module = importlib.import_module(module_path)

                if hasattr(service_module, class_name):
                    service_class = getattr(service_module, class_name)
                    instance = service_class(info.context.user)

                    serializer_method = getattr(instance, method_name, None)

                    if callable(serializer_method):
                        serialized_data = serializer_method(serialized_data)

            except ImportError:
                return f"Error: Module '{module_path}' not found."
            except AttributeError:
                return f"Error: Attribute not found in the module or class."
            except Exception as e:
                return f"Error: {str(e)}"

        return serialized_data

    def resolve_entity_string(self, info):
        return self.entity.__str__()

    @classmethod
    def get_queryset(cls, queryset, info):
        user = info.context.user
        if user.is_imis_admin or is_task_triage(user):
            return queryset.filter(is_deleted=False)
        return queryset.filter(
            Q(task_group__taskexecutor__user=user) & ~Q(status=Task.Status.RECEIVED),
            is_deleted=False
        )


class TaskHistoryGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    business_data = graphene.JSONString()
    entity_string = graphene.String()

    class Meta:
        model = Task.history.model
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "entity_type": ["exact"],
            "entity_id": ["exact"],

            "source": ["exact", "iexact", "istartswith", "icontains"],
            "status": ["exact", "iexact", "istartswith", "icontains"],
            "executor_action_event": ["exact", "iexact", "istartswith", "icontains"],
            "business_event": ["exact", "iexact", "istartswith", "icontains"],

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_business_data(self, info):
        data = self.data
        serializer_path = self.business_data_serializer
        serialized_data = copy.deepcopy(data)
        if serializer_path:
            module_path, class_name, method_name = serializer_path.rsplit('.', 2)

            try:
                service_module = importlib.import_module(module_path)

                if hasattr(service_module, class_name):
                    service_class = getattr(service_module, class_name)
                    instance = service_class(info.context.user)

                    serializer_method = getattr(instance, method_name, None)

                    if callable(serializer_method):
                        serialized_data = serializer_method(serialized_data)

            except ImportError:
                return f"Error: Module '{module_path}' not found."
            except AttributeError:
                return f"Error: Attribute not found in the module or class."
            except Exception as e:
                return f"Error: {str(e)}"

        return serialized_data

    def resolve_entity_string(self, info):
        return entity_string_of(self.entity_type_id, self.entity_id)

    @classmethod
    def get_queryset(cls, queryset, info):
        user = info.context.user
        if user.is_imis_admin or is_task_triage(user):
            return queryset.filter(is_deleted=False)
        return queryset.filter(
            Q(task_group__taskexecutor__user=user) & ~Q(status=Task.Status.RECEIVED),
            is_deleted=False
        )


class TaskGroupGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    user = graphene.List(UserGQLType)

    class Meta:
        model = TaskGroup
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "code": ["exact", "iexact", "startswith", "istartswith", "contains", "icontains"],
            "completion_policy": ["exact", "iexact"],

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_user(self, info):
        task_group_id = self.id
        return TaskExecutor.objects.filter(task_group_id=task_group_id)


class TaskExecutorGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = TaskExecutor
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            **prefix_filterset("user__", UserGQLType._meta.filter_fields),
            **prefix_filterset("task_group__", TaskGroupGQLType._meta.filter_fields),
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class TaskFlowStepGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    # Resolved inherit-or-override values so the FE can render
    # "Inherit (ALL)" without re-implementing the inheritance rule.
    effective_policy = graphene.String()
    effective_threshold = graphene.Int()

    class Meta:
        model = TaskFlowStep
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "order": ["exact"],
            "completion_policy": ["exact", "iexact"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_effective_policy(self, info):
        return self.effective_policy()

    def resolve_effective_threshold(self, info):
        return self.effective_threshold()


class TaskFlowGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    task_sources = graphene.List(graphene.String)
    steps = graphene.List(TaskFlowStepGQLType)
    step_count = graphene.Int()
    in_flight_count = graphene.Int()

    class Meta:
        model = TaskFlow
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "code": ["exact", "iexact", "startswith", "istartswith", "contains", "icontains"],
            "name": ["exact", "iexact", "startswith", "istartswith", "contains", "icontains"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte", "isnull"],
            "replacement_uuid": ["exact", "isnull"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_task_sources(self, info):
        return (self.json_ext or {}).get('task_sources', [])

    def resolve_steps(self, info):
        return self.steps.filter(is_deleted=False).select_related('task_group').order_by('order')

    def resolve_step_count(self, info):
        return self.steps.filter(is_deleted=False).count()

    def resolve_in_flight_count(self, info):
        return Task.objects.filter(
            flow_id=self.id, is_deleted=False,
            status__in=[Task.Status.RECEIVED, Task.Status.ACCEPTED],
        ).count()


class TaskAssignmentTargetGQLType(graphene.ObjectType):
    """
    One row of the task assignment picker.

    Assigning a task is a single decision with two shapes - an ordered
    approval flow or a flat task group - so both are served from one query,
    tagged with `kind`, instead of making the client merge and paginate two
    lists. Only assignable targets are returned: a flow without steps or a
    superseded version would be rejected by the mutation, so it is never
    offered.
    """

    class Kind(graphene.Enum):
        FLOW = 'FLOW'
        GROUP = 'GROUP'

    kind = graphene.Field(Kind, required=True)
    uuid = graphene.String(required=True)
    code = graphene.String()
    name = graphene.String()
    # FLOW only
    step_count = graphene.Int()
    # GROUP only
    completion_policy = graphene.String()
    threshold = graphene.Int()
    member_count = graphene.Int()

    @classmethod
    def from_flow(cls, flow, step_count):
        return cls(
            kind='FLOW', uuid=str(flow.id), code=flow.code, name=flow.name,
            step_count=step_count,
        )

    @classmethod
    def from_group(cls, group, member_count):
        return cls(
            kind='GROUP', uuid=str(group.id), code=group.code, name=group.code,
            completion_policy=group.completion_policy, threshold=group.threshold,
            member_count=member_count,
        )


class TaskDecisionGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = TaskDecision
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "decision": ["exact", "iexact"],
            "record_id": ["exact", "isnull"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            **prefix_filterset("task__", TaskGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        # Same visibility rule as tasks: privileged users see everything,
        # executors see the decisions of tasks assigned to their groups.
        # taskexecutor__is_deleted=False: a removed membership must not keep
        # granting visibility into a task's decision ledger. distinct() is
        # required once that join is added - a task can match through more
        # than one live executor row for the same user/group pairing history,
        # which would otherwise duplicate every decision node.
        # task__is_deleted=False on both branches: a deleted task is invisible
        # in the task queryset, so its decision ledger must not remain
        # readable either.
        user = info.context.user
        if user.is_imis_admin or is_task_triage(user):
            return queryset.filter(is_deleted=False, task__is_deleted=False)
        return queryset.filter(
            Q(task__task_group__taskexecutor__user=user,
              task__task_group__taskexecutor__is_deleted=False)
            & ~Q(task__status=Task.Status.RECEIVED),
            is_deleted=False,
            task__is_deleted=False,
        ).distinct()
