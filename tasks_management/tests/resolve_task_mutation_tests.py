import json
import uuid
from unittest import skipUnless

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db.models.signals import pre_save

from core.models import MutationLog
from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase, BaseTestContext
from core.test_helpers import LogInHelper
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task, TaskGroup, TaskMutation
from tasks_management.services import TaskGroupService, TaskService

REFUSAL = 'tasks_management.test.completion_refused'

RESOLVE_TASK_MUTATION = """
mutation ResolveTask($input: ResolveTaskMutationInput!) {
  resolveTask(input: $input) { internalId clientMutationId }
}
"""


class ResolveTaskMutationCompletionTest(openIMISGraphQLTestCase):
    """resolveTask reports the outcome of the completion its approval triggers."""

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()
        cls.token = BaseTestContext(user=cls.user).get_jwt()

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.accepting_source = f'resolve_accepting_{suffix}'
        self.refusing_source = f'resolve_refusing_{suffix}'
        pre_save.connect(self._refuse_completion, sender=Task, dispatch_uid=f'refuse_completion_{suffix}')
        self.addCleanup(pre_save.disconnect, sender=Task, dispatch_uid=f'refuse_completion_{suffix}')

    def _refuse_completion(self, sender, instance, **kwargs):
        if instance.source == self.refusing_source \
                and instance.status in (Task.Status.COMPLETED, Task.Status.FAILED):
            raise ValidationError(REFUSAL)

    def _group(self, policy, sources, threshold=None):
        payload = {
            'code': f'resolve_{policy}_{uuid.uuid4().hex[:8]}',
            'completion_policy': policy,
            'user_ids': [str(self.user.id)],
            'task_sources': sources,
        }
        if threshold is not None:
            payload['threshold'] = threshold
        result = TaskGroupService(self.user).create(payload)
        self.assertTrue(result['success'], result)
        return TaskGroup.objects.get(id=result['data']['id'])

    def _task(self, source):
        result = TaskService(self.user).create({
            'source': source,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': 'ResolveTest.update',
            'data': {},
        })
        self.assertTrue(result['success'], result)
        task = Task.objects.get(id=result['data']['id'])
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        return task

    def _resolve(self, task, decision, additional_data=None):
        client_mutation_id = str(uuid.uuid4())
        mutation_input = {
            'id': str(task.id),
            'businessStatus': json.dumps({str(self.user.id): decision}),
            'clientMutationId': client_mutation_id,
        }
        if additional_data is not None:
            mutation_input['additionalData'] = json.dumps(additional_data)
        response = self.query(
            RESOLVE_TASK_MUTATION,
            variables={'input': mutation_input},
            headers={'HTTP_AUTHORIZATION': f'Bearer {self.token}'},
        )
        self.assertResponseNoErrors(response)
        return MutationLog.objects.get(client_mutation_id=client_mutation_id)

    def test_an_approval_that_completes_the_task_is_logged_as_a_success(self):
        self._group(TaskGroup.TaskGroupCompletionPolicy.ANY, [self.accepting_source])
        task = self._task(self.accepting_source)

        log = self._resolve(task, 'APPROVED', {'note': 'fine'})

        self.assertEqual(log.status, MutationLog.SUCCESS, log.error)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertEqual(task.business_status, {str(self.user.id): 'APPROVED'})
        self.assertEqual(task.json_ext['additional_resolve_data'][str(self.user.id)], {'note': 'fine'})

    def test_a_refused_completion_marks_the_mutation_failed_with_the_refusal(self):
        self._group(TaskGroup.TaskGroupCompletionPolicy.ANY, [self.refusing_source])
        task = self._task(self.refusing_source)

        log = self._resolve(task, 'APPROVED')

        self.assertEqual(log.status, MutationLog.ERROR)
        error = json.loads(log.error)
        self.assertFalse(error['success'])
        self.assertIn(REFUSAL, error['detail'])

    def test_a_refused_completion_leaves_the_task_as_it_was_before_the_resolve(self):
        self._group(TaskGroup.TaskGroupCompletionPolicy.ANY, [self.refusing_source])
        task = self._task(self.refusing_source)
        history_rows = Task.history.filter(id=task.id).count()

        self._resolve(task, 'APPROVED', {'note': 'refused'})

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        self.assertNotIn(str(self.user.id), task.business_status)
        self.assertNotIn(str(self.user.id), (task.json_ext or {}).get('additional_resolve_data', {}))
        self.assertEqual(Task.history.filter(id=task.id).count(), history_rows)

    def test_a_refused_completion_keeps_the_mutation_linked_to_the_task(self):
        self._group(TaskGroup.TaskGroupCompletionPolicy.ANY, [self.refusing_source])
        task = self._task(self.refusing_source)

        log = self._resolve(task, 'APPROVED')

        self.assertTrue(TaskMutation.objects.filter(task=task, mutation=log).exists())

    def test_a_refused_failure_vote_marks_the_mutation_failed(self):
        self._group(TaskGroup.TaskGroupCompletionPolicy.ANY, [self.refusing_source])
        task = self._task(self.refusing_source)

        log = self._resolve(task, 'FAILED')

        self.assertEqual(log.status, MutationLog.ERROR)
        self.assertIn(REFUSAL, json.loads(log.error)['detail'])
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.ACCEPTED)

    def test_a_refused_completion_under_policy_all_marks_the_mutation_failed(self):
        self._group(TaskGroup.TaskGroupCompletionPolicy.ALL, [self.refusing_source])
        task = self._task(self.refusing_source)

        log = self._resolve(task, 'APPROVED')

        self.assertEqual(log.status, MutationLog.ERROR)
        self.assertIn(REFUSAL, json.loads(log.error)['detail'])
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        self.assertEqual(task.business_status, {})

    def test_a_refused_completion_under_policy_n_marks_the_mutation_failed(self):
        self._group(TaskGroup.TaskGroupCompletionPolicy.N, [self.refusing_source], threshold=1)
        task = self._task(self.refusing_source)

        log = self._resolve(task, 'APPROVED')

        self.assertEqual(log.status, MutationLog.ERROR)
        self.assertIn(REFUSAL, json.loads(log.error)['detail'])
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        self.assertEqual(task.business_status, {})

    def test_a_retry_after_the_refusal_is_lifted_completes_the_task(self):
        self._group(TaskGroup.TaskGroupCompletionPolicy.ANY, [self.refusing_source])
        task = self._task(self.refusing_source)
        self._resolve(task, 'APPROVED')
        self.refusing_source = None

        log = self._resolve(task, 'APPROVED')

        self.assertEqual(log.status, MutationLog.SUCCESS, log.error)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)

    def test_the_service_raises_the_refused_completion_to_direct_callers(self):
        from tasks_management.services import TaskCompletionRefused
        self._group(TaskGroup.TaskGroupCompletionPolicy.ANY, [self.refusing_source])
        task = self._task(self.refusing_source)

        with self.assertRaises(TaskCompletionRefused) as refused:
            TaskService(self.user).resolve_task({
                'id': task.id, 'business_status': {str(self.user.id): 'APPROVED'},
            })

        self.assertFalse(refused.exception.result['success'])
        self.assertIn(REFUSAL, refused.exception.result['detail'])
        task.refresh_from_db()
        self.assertEqual(task.business_status, {})


@skipUnless(apps.is_installed('deduplication'), 'needs the deduplication module and its Task pre_save bridge')
class ResolveTaskMutationDeduplicationRefusalTest(openIMISGraphQLTestCase):
    """The deduplication bridge refuses a completion whose merge it cannot apply."""

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()
        cls.token = BaseTestContext(user=cls.user).get_jwt()

    def test_a_refused_duplicate_merge_marks_the_resolve_failed_and_keeps_the_pair_open(self):
        from deduplication.models import DuplicateCandidate
        from deduplication.services import DUPLICATE_CANDIDATE_TASK_SOURCE, create_review_tasks
        from deduplication.sources import order_pair
        from individual.models import Individual

        group = TaskGroupService(self.user).create({
            'code': f'dedup_review_{uuid.uuid4().hex[:8]}',
            'completion_policy': TaskGroup.TaskGroupCompletionPolicy.ANY,
            'user_ids': [str(self.user.id)],
            'task_sources': [DUPLICATE_CANDIDATE_TASK_SOURCE],
        })
        self.assertTrue(group['success'], group)
        kept = Individual(first_name='A', last_name='One', dob='1990-01-01')
        kept.save(username=self.user.username)
        other = Individual(first_name='B', last_name='Two', dob='1990-01-01')
        other.save(username=self.user.username)
        subject_a, subject_b = order_pair(str(kept.id), str(other.id))
        candidate = DuplicateCandidate.objects.create(
            subject_model='individual.Individual', subject_a=subject_a, subject_b=subject_b,
            kind='demographic', source='TestSource', evidence={},
        )
        create_review_tasks([candidate.id], self.user)
        candidate.refresh_from_db()
        task = Task.objects.get(id=candidate.task_id)
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        kept.delete(user=self.user)

        client_mutation_id = str(uuid.uuid4())
        response = self.query(
            RESOLVE_TASK_MUTATION,
            variables={'input': {
                'id': str(task.id),
                'businessStatus': json.dumps({str(self.user.id): 'APPROVED'}),
                'additionalData': json.dumps({'decision': 'same', 'keep': str(kept.id)}),
                'clientMutationId': client_mutation_id,
            }},
            headers={'HTTP_AUTHORIZATION': f'Bearer {self.token}'},
        )
        self.assertResponseNoErrors(response)

        log = MutationLog.objects.get(client_mutation_id=client_mutation_id)
        self.assertEqual(log.status, MutationLog.ERROR)
        self.assertIn('deduplication.resolve.subject_deleted', json.loads(log.error)['detail'])
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.ACCEPTED)
        self.assertNotIn(str(self.user.id), task.business_status)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.OPEN)
