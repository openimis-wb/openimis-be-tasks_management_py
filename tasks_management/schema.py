import graphene
import json
import graphene_django_optimizer as gql_optimizer

from django.contrib.auth.models import AnonymousUser
from django.db.models import Q

from core.schema import OrderedDjangoFilterConnectionField
from core.services import wait_for_mutation
from core.utils import append_validity_filter
from tasks_management.gql_mutations import CreateTaskGroupMutation, UpdateTaskGroupMutation, DeleteTaskGroupMutation, \
    UpdateTaskMutation, ResolveTaskMutation
from tasks_management.gql_queries import TaskGroupGQLType, TaskExecutorGQLType, TaskGQLType, TaskHistoryGQLType, \
    entity_string_of
from tasks_management.models import TaskGroup, TaskExecutor, Task
from tasks_management.apps import TasksManagementConfig


class Query(graphene.ObjectType):
    module_name = "tasks_management"

    task_group = OrderedDjangoFilterConnectionField(
        TaskGroupGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        client_mutation_id=graphene.String(),
        search=graphene.String(),
    )
    task_executor = OrderedDjangoFilterConnectionField(
        TaskExecutorGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        client_mutation_id=graphene.String(),
        taskGroupIdString=graphene.String(),
    )

    task = OrderedDjangoFilterConnectionField(
        TaskGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        groupId=graphene.String(),
        customFilters=graphene.List(of_type=graphene.String),
        taskGroupId=graphene.String(),
        entityIds=graphene.List(graphene.UUID),
        entityString__Icontains=graphene.String(),
    )
    task_history = OrderedDjangoFilterConnectionField(
        TaskHistoryGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        groupId=graphene.String(),
        customFilters=graphene.List(of_type=graphene.String),
        taskGroupId=graphene.String(),
        entityIds=graphene.List(graphene.UUID),
        entityString__Icontains=graphene.String(),
    )

    def resolve_task(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        taskGroupId = kwargs.get("taskGroupId")
        if taskGroupId:
            filters.append(Q(task_group__id=taskGroupId))

        entityIds = kwargs.get("entityIds")
        if entityIds:
            filters.append(Q(entity_id__in=entityIds))

        # not checking perms because get_queryset filters tasks assigned to user
        query = Task.objects.filter(*filters)

        entity_string = kwargs.get("entityString__Icontains")
        if entity_string:
            task_ids = [task.id for task in query if entity_string.lower() in str(task.entity).lower()]
            query = query.filter(id__in=task_ids)

        return gql_optimizer.query(query, info)

    def resolve_task_history(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        taskGroupId = kwargs.get("taskGroupId")
        if taskGroupId:
            filters.append(Q(task_group__id=taskGroupId))

        entityIds = kwargs.get("entityIds")
        if entityIds:
            filters.append(Q(entity_id__in=entityIds))

        # not checking perms because get_queryset filters tasks assigned to user
        query = Task.history.filter(*filters)

        entity_string = kwargs.get("entityString__Icontains")
        if entity_string:
            task_ids = [
                task.id for task in query
                if entity_string.lower() in (entity_string_of(task.entity_type_id, task.entity_id) or '').lower()
            ]
            query = query.filter(id__in=task_ids)

        return gql_optimizer.query(query, info)

    def resolve_task_group(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        search = kwargs.get("search", None)
        if search is not None:
            filters.append(
                Q(code__icontains=search)
                | Q(taskexecutor__user__i_user__last_name__icontains=search)
                | Q(taskexecutor__user__i_user__other_names__icontains=search)
            )

        query = TaskGroup.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_task_executor(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        task_group_id_string = kwargs.get("taskGroupIdString")
        if task_group_id_string:
            filters.append(Q(taskgroup__user__id_icontains=task_group_id_string))

        Query._check_permissions(info.context.user, TasksManagementConfig.gql_task_group_search_perms)
        query = TaskExecutor.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    @staticmethod
    def _check_permissions(user, perms):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(perms):
            raise PermissionError("Unauthorized")


class Mutation(graphene.ObjectType):
    create_task_group = CreateTaskGroupMutation.Field()
    update_task_group = UpdateTaskGroupMutation.Field()
    delete_task_group = DeleteTaskGroupMutation.Field()

    update_task = UpdateTaskMutation.Field()
    resolve_task = ResolveTaskMutation.Field()
