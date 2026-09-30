import graphene
import json
import graphene_django_optimizer as gql_optimizer

from django.contrib.auth.models import AnonymousUser
from django.db.models import Count, Q

from core.schema import OrderedDjangoFilterConnectionField
from core.services import wait_for_mutation
from core.utils import append_validity_filter
from tasks_management.gql_mutations import CreateTaskGroupMutation, UpdateTaskGroupMutation, DeleteTaskGroupMutation, \
    UpdateTaskMutation, ResolveTaskMutation, CreateTaskFlowMutation, UpdateTaskFlowMutation, \
    ReplaceTaskFlowMutation, DeleteTaskFlowMutation
from tasks_management.gql_queries import TaskGroupGQLType, TaskExecutorGQLType, TaskGQLType, TaskHistoryGQLType, \
    TaskFlowGQLType, TaskDecisionGQLType, TaskAssignmentTargetGQLType, entity_string_of
from tasks_management.models import TaskGroup, TaskExecutor, Task, TaskFlow, TaskDecision
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
    task_flow = OrderedDjangoFilterConnectionField(
        TaskFlowGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        client_mutation_id=graphene.String(),
        search=graphene.String(),
        showSuperseded=graphene.Boolean(),
    )
    task_decision = OrderedDjangoFilterConnectionField(
        TaskDecisionGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        taskId=graphene.UUID(),
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

    task_assignment_targets = graphene.List(
        TaskAssignmentTargetGQLType,
        search=graphene.String(),
        first=graphene.Int(),
        include_flows=graphene.Boolean(),
        include_groups=graphene.Boolean(),
    )

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

    def resolve_task_flow(self, info, **kwargs):
        Query._check_permissions(info.context.user, TasksManagementConfig.gql_task_flow_search_perms)
        filters = []

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)

        # Head versions by default; showSuperseded exposes version history.
        if not kwargs.get("showSuperseded"):
            filters.append(Q(replacement_uuid__isnull=True))

        search = kwargs.get("search")
        if search is not None:
            filters.append(Q(code__icontains=search) | Q(name__icontains=search))

        query = TaskFlow.objects.filter(*filters, is_deleted=False)
        return gql_optimizer.query(query, info)

    def resolve_task_assignment_targets(
        self, info, search=None, first=20, include_flows=True, include_groups=True,
    ):
        """
        Serves the single "assigned to" picker. `first` caps each kind, not
        the combined list, so a long list of groups can never crowd the flows
        out of the dropdown.
        """
        Query._check_permissions(info.context.user, TasksManagementConfig.gql_task_search_perms)
        targets = []

        # Flows are only offered to users who may see them at all; everyone
        # else simply gets the groups, and the picker degrades to its old self.
        may_see_flows = info.context.user.has_perms(
            TasksManagementConfig.gql_task_flow_search_perms)
        if include_flows and may_see_flows:
            flows = TaskFlow.objects.filter(
                is_deleted=False, replacement_uuid__isnull=True,
            ).annotate(
                live_step_count=Count('steps', filter=Q(steps__is_deleted=False)),
            ).filter(live_step_count__gt=0)
            if search:
                flows = flows.filter(Q(code__icontains=search) | Q(name__icontains=search))
            targets += [
                TaskAssignmentTargetGQLType.from_flow(flow, flow.live_step_count)
                for flow in flows.order_by('code')[:first]
            ]

        if include_groups:
            groups = TaskGroup.objects.filter(is_deleted=False).annotate(
                live_member_count=Count(
                    'taskexecutor', filter=Q(taskexecutor__is_deleted=False)),
            )
            if search:
                groups = groups.filter(code__icontains=search)
            targets += [
                TaskAssignmentTargetGQLType.from_group(group, group.live_member_count)
                for group in groups.order_by('code')[:first]
            ]

        return targets

    def resolve_task_decision(self, info, **kwargs):
        # No perms check: TaskDecisionGQLType.get_queryset scopes rows to the
        # caller's task visibility, mirroring the task query.
        filters = []
        task_id = kwargs.get("taskId")
        if task_id:
            filters.append(Q(task__id=task_id))
        query = TaskDecision.objects.filter(*filters)
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

    create_task_flow = CreateTaskFlowMutation.Field()
    update_task_flow = UpdateTaskFlowMutation.Field()
    replace_task_flow = ReplaceTaskFlowMutation.Field()
    delete_task_flow = DeleteTaskFlowMutation.Field()
