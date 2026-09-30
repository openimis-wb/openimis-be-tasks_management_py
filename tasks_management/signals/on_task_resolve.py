import logging

from core.forms import User
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task
from tasks_management.services import TaskService, TaskCompletionRefused

logger = logging.getLogger(__name__)


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
    # TODO for now hardcoded to any, to be updated
    resolve_task_any(_task, _user)


def on_task_resolve(**kwargs):
    """
    Generic event for checking the completion_policy of a task. if the task is completed or failed,
    TaskService.complete_task is called with appropriate `failed` flag. A failed completion raises
    TaskCompletionRefused out of TaskService.resolve_task.
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
    except TaskCompletionRefused:
        raise
    except Exception as e:
        logger.error("Error while executing on_task_resolve", exc_info=e)
        return [str(e)]
