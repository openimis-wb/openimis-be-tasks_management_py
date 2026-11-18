import logging

from core.forms import User
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task
from tasks_management.services import TaskService

logger = logging.getLogger(__name__)


def resolve_task_all(_task, _user):
    if 'FAILED' in _task.business_status.values():
        TaskService(_user).complete_task({"id": _task.id, 'failed': True})
        return
    n_of_approves = sum(map('APPROVED'.__eq__, _task.business_status.values()))
    n_of_executors = _task.task_group.taskexecutor_set.filter(task_group__is_deleted=False, is_deleted=False).count()
    if not n_of_executors:
        logger.warning("No valid executors of task with policy ALL %s", str(_task.uuid))
    if n_of_approves == n_of_executors:
        TaskService(_user).complete_task({"id": _task.id})


def resolve_task_any(_task, _user):
    if 'FAILED' in _task.business_status.values():
        TaskService(_user).complete_task({"id": _task.id, 'failed': True})
        return
    if 'APPROVED' in _task.business_status.values():
        TaskService(_user).complete_task({"id": _task.id})


def resolve_task_n(_task, _user):
    # TODO for now hardcoded to any, to be updated
    resolve_task_any(_task, _user)


def on_task_resolve(**kwargs):
    """
    Generic event for checking the completion_policy of a task. if the task is completed or failed,
    TaskService.complete_task is called with appropriate `failed` flag.
    """
    try:
        result = kwargs.get('result', None)
        if result and result['success'] \
                and result['data']['task']['status'] == Task.Status.ACCEPTED \
                and result['data']['task']['executor_action_event'] == TasksManagementConfig.default_executor_event:
            data = kwargs.get("result").get("data")
            task = Task.objects.select_related('task_group').prefetch_related('task_group__taskexecutor_set').get(
                id=data["task"]["id"])
            user = User.objects.get(id=data["user"]["id"])

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
    except Exception as e:
        logger.error("Error while executing on_task_resolve", exc_info=e)
        return [str(e)]
