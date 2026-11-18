from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.service_signals import ServiceSignalBindType
from core.signals import REGISTERED_SERVICE_SIGNALS, bind_service_signal
from core.test_helpers import create_test_interactive_user
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task, TaskExecutor, TaskGroup
from tasks_management.services import TaskGroupService, TaskService
from tasks_management.signals.on_task_resolve import resolve_task_all

APPROVED = 'APPROVED'
FAILED = 'FAILED'
COMPLETE_SIGNAL = 'task_service.complete_task'


class TaskVoteValidationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.executor = create_test_interactive_user(username='vote_executor')
        cls.second_executor = create_test_interactive_user(username='vote_second_executor')
        cls.outsider = create_test_interactive_user(username='vote_outsider')
        cls.group_any = cls._create_group('vote_any', TaskGroup.TaskGroupCompletionPolicy.ANY,
                                          [cls.executor, cls.second_executor])
        cls.group_all = cls._create_group('vote_all', TaskGroup.TaskGroupCompletionPolicy.ALL,
                                          [cls.executor, cls.second_executor])

    def setUp(self):
        self.completions = []

        def record_completion(**kwargs):
            self.completions.append(kwargs['result'])

        # Other test classes replace the registered signal, so the receiver binds per test.
        bind_service_signal(COMPLETE_SIGNAL, record_completion, ServiceSignalBindType.AFTER)
        self.addCleanup(REGISTERED_SERVICE_SIGNALS[COMPLETE_SIGNAL].after_service_signal.disconnect,
                        record_completion)
        self.record_completion = record_completion

    @classmethod
    def _create_group(cls, code, policy, executors):
        result = TaskGroupService(cls.executor).create({
            'code': code,
            'completion_policy': policy,
            'user_ids': [str(executor.id) for executor in executors],
        })
        return TaskGroup.objects.get(id=result['data']['id'])

    def _task(self, group=None, status=Task.Status.ACCEPTED, business_status=None):
        task = Task(source='vote_test', status=status, task_group=group,
                    executor_action_event=TasksManagementConfig.default_executor_event,
                    business_event='tasks_management.vote_test',
                    business_status=business_status or {}, json_ext={})
        task.save(username=self.executor.login_name)
        return task

    def _vote(self, voter, task, value=APPROVED, key=None):
        return TaskService(voter).resolve_task({
            'id': task.id,
            'business_status': {str(key or voter.id): value},
        })

    def _assert_refused(self, result, task, status=Task.Status.ACCEPTED, business_status=None):
        self.assertFalse(result['success'], result)
        self.assertTrue(result['detail'], result)
        task.refresh_from_db()
        self.assertEqual(task.status, status)
        self.assertEqual(task.business_status, business_status or {})
        self.assertEqual(self.completions, [])

    def _successful_completions(self):
        return [result for result in self.completions if result['success']]

    def test_executor_vote_completes_task(self):
        task = self._task(self.group_any)

        result = self._vote(self.executor, task)

        self.assertTrue(result['success'], result)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertEqual(task.business_status, {str(self.executor.id): APPROVED})
        self.assertEqual(len(self._successful_completions()), 1)

    def test_all_policy_waits_for_every_executor(self):
        task = self._task(self.group_all)

        self.assertTrue(self._vote(self.executor, task)['success'])
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        self.assertEqual(self.completions, [])

        self.assertTrue(self._vote(self.second_executor, task)['success'])
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertEqual(len(self._successful_completions()), 1)

    def test_vote_from_non_executor_is_refused(self):
        task = self._task(self.group_any)

        self._assert_refused(self._vote(self.outsider, task), task)

    def test_vote_from_non_executor_is_refused_when_policy_needs_one_approval(self):
        group = self._create_group('vote_all_single', TaskGroup.TaskGroupCompletionPolicy.ALL, [self.executor])
        task = self._task(group)

        self._assert_refused(self._vote(self.outsider, task), task)

    def test_vote_from_removed_executor_is_refused(self):
        TaskExecutor.objects.filter(task_group=self.group_any, user=self.second_executor).update(is_deleted=True)
        task = self._task(self.group_any)

        self._assert_refused(self._vote(self.second_executor, task), task)

    def test_vote_on_task_of_deleted_group_is_refused(self):
        TaskGroup.objects.filter(id=self.group_any.id).update(is_deleted=True)
        task = self._task(self.group_any)

        self._assert_refused(self._vote(self.executor, task), task)

    def test_vote_on_task_without_group_is_refused(self):
        task = self._task()

        self._assert_refused(self._vote(self.executor, task), task)

    def test_non_executor_cannot_vote_under_an_executor_key(self):
        task = self._task(self.group_any)

        self._assert_refused(self._vote(self.outsider, task, key=self.executor.id), task)

    def test_executor_cannot_vote_for_another_executor(self):
        task = self._task(self.group_all)

        self._assert_refused(self._vote(self.executor, task, key=self.second_executor.id), task)

    def test_executor_cannot_add_a_vote_under_another_key(self):
        task = self._task(self.group_all)

        result = TaskService(self.executor).resolve_task({
            'id': task.id,
            'business_status': {str(self.executor.id): APPROVED, str(self.second_executor.id): APPROVED},
        })

        self._assert_refused(result, task)

    def test_vote_on_received_task_is_refused(self):
        task = self._task(self.group_any, status=Task.Status.RECEIVED)

        self._assert_refused(self._vote(self.executor, task), task, status=Task.Status.RECEIVED)

    def test_vote_on_completed_task_is_refused(self):
        task = self._task(self.group_any, status=Task.Status.COMPLETED)

        self._assert_refused(self._vote(self.executor, task), task, status=Task.Status.COMPLETED)

    def test_triage_by_non_executor_still_accepts_task(self):
        task = self._task(status=Task.Status.RECEIVED)

        result = TaskService(self.outsider).update({
            'id': task.id,
            'status': Task.Status.ACCEPTED,
            'task_group_id': self.group_any.id,
        })

        self.assertTrue(result['success'], result)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        self.assertEqual(task.task_group_id, self.group_any.id)

    def test_triage_with_string_ids_still_accepts_task(self):
        task = self._task(status=Task.Status.RECEIVED)

        result = TaskService(self.outsider).update({
            'id': str(task.id),
            'status': 'ACCEPTED',
            'task_group_id': str(self.group_any.id),
        })

        self.assertTrue(result['success'], result)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        self.assertEqual(task.task_group_id, self.group_any.id)

    def _update(self, user, task, **fields):
        return TaskService(user).update({'id': task.id, **fields})

    def _assert_update_refused(self, result, task, status, group):
        self.assertFalse(result['success'], result)
        self.assertTrue(result['detail'], result)
        task.refresh_from_db()
        self.assertEqual(task.status, status)
        self.assertEqual(task.task_group_id, group.id if group else None)
        self.assertEqual(self.completions, [])

    def test_non_executor_cannot_move_accepted_task_into_own_group_and_vote(self):
        own_group = self._create_group('vote_outsider_group', TaskGroup.TaskGroupCompletionPolicy.ANY,
                                       [self.outsider])
        task = self._task(self.group_any)

        moved = self._update(self.outsider, task, status=Task.Status.ACCEPTED, task_group_id=own_group.id)

        self._assert_update_refused(moved, task, Task.Status.ACCEPTED, self.group_any)
        self._assert_refused(self._vote(self.outsider, task), task)

    def test_update_cannot_complete_accepted_task(self):
        for final_status in (Task.Status.COMPLETED, Task.Status.FAILED):
            with self.subTest(final_status=final_status):
                task = self._task(self.group_any)

                result = self._update(self.outsider, task, status=final_status, task_group_id=self.group_any.id)

                self._assert_update_refused(result, task, Task.Status.ACCEPTED, self.group_any)

    def test_update_cannot_complete_received_task(self):
        task = self._task(status=Task.Status.RECEIVED)

        result = self._update(self.executor, task, status=Task.Status.COMPLETED)

        self._assert_update_refused(result, task, Task.Status.RECEIVED, None)

    def test_update_cannot_reset_accepted_task_with_null_status_or_group(self):
        for fields in ({'status': None}, {'task_group_id': None}):
            with self.subTest(fields=fields):
                task = self._task(self.group_any)

                result = self._update(self.outsider, task, **fields)

                self._assert_update_refused(result, task, Task.Status.ACCEPTED, self.group_any)

    def test_saving_accepted_task_unchanged_succeeds(self):
        for group_id in (self.group_any.id, str(self.group_any.id)):
            with self.subTest(group_id=type(group_id).__name__):
                task = self._task(self.group_any)

                result = self._update(self.outsider, task, status='ACCEPTED', task_group_id=group_id)

                self.assertTrue(result['success'], result)
                task.refresh_from_db()
                self.assertEqual(task.status, Task.Status.ACCEPTED)
                self.assertEqual(task.task_group_id, self.group_any.id)

    def test_executor_can_vote_again_with_partial_decisions(self):
        task = self._task(self.group_any)
        key = str(self.executor.id)

        self.assertTrue(self._vote(self.executor, task, value={'ACCEPT': ['a']})['success'])
        self.assertTrue(self._vote(self.executor, task, value={'ACCEPT': ['b'], 'REJECT': ['c']})['success'])

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        self.assertEqual(task.business_status, {key: {'ACCEPT': ['a', 'b'], 'REJECT': ['c']}})

    def test_complete_task_twice_fires_business_event_once(self):
        task = self._task(self.group_any)
        service = TaskService(self.executor)

        first = service.complete_task({'id': task.id})
        second = service.complete_task({'id': task.id})

        self.assertTrue(first['success'], first)
        self.assertFalse(second['success'], second)
        self.assertEqual(len(self._successful_completions()), 1)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)

    def test_completed_task_cannot_become_failed(self):
        task = self._task(self.group_any)
        service = TaskService(self.executor)

        service.complete_task({'id': task.id})
        second = service.complete_task({'id': task.id, 'failed': True})

        self.assertFalse(second['success'], second)
        self.assertEqual(len(self._successful_completions()), 1)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)

    def test_complete_task_still_accepts_received_task(self):
        task = self._task(status=Task.Status.RECEIVED)

        result = TaskService(self.executor).complete_task({'id': task.id})

        self.assertTrue(result['success'], result)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)

    def test_any_policy_with_failed_and_approved_completes_once_as_failed(self):
        task = self._task(self.group_any, business_status={str(self.executor.id): APPROVED})

        result = self._vote(self.second_executor, task, value=FAILED)

        self.assertTrue(result['success'], result)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertEqual(len(self._successful_completions()), 1)
        self.assertEqual(self._successful_completions()[0]['data']['task']['status'], Task.Status.FAILED)

    def test_all_policy_without_executors_completes_once_as_failed(self):
        group = self._create_group('vote_all_empty', TaskGroup.TaskGroupCompletionPolicy.ALL, [])
        task = self._task(group, business_status={str(self.executor.id): FAILED})

        resolve_task_all(task, self.executor)

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertEqual(len(self._successful_completions()), 1)

    def test_vote_and_completion_lock_the_task_row(self):
        task = self._task(self.group_all)
        locked = Task._meta.db_table

        for call in (lambda: self._vote(self.executor, task),
                     lambda: TaskService(self.executor).complete_task({'id': task.id})):
            with CaptureQueriesContext(connection) as queries:
                self.assertTrue(call()['success'])
            self.assertTrue(
                any(locked in query['sql'] and 'FOR UPDATE' in query['sql'] for query in queries),
                [query['sql'] for query in queries])
