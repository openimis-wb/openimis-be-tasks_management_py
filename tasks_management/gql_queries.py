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
from tasks_management.models import TaskGroup, TaskExecutor, Task

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
