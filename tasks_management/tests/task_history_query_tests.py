import json

from core.models import InteractiveUser
from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase, BaseTestContext
from core.test_helpers import LogInHelper
from tasks_management.models import Task
from tasks_management.services import TaskService


class TaskHistoryEntityStringTest(openIMISGraphQLTestCase):
    """taskHistory rows expose and filter on the entity of their task."""

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()
        cls.token = BaseTestContext(user=cls.user).get_jwt()
        cls.entity = InteractiveUser.objects.get(id=cls.user.i_user_id)
        cls.task = cls._task('history_entity_source', cls.entity)
        cls.task_without_entity = cls._task('history_entity_source', None)

    @classmethod
    def _task(cls, source, entity):
        service = TaskService(cls.user)
        result = service.create({
            'source': source,
            'entity': entity,
            'executor_action_event': 'history_test',
            'business_event': 'HistoryTest.update',
            'data': {},
        })
        assert result['success'], result
        service.update({'id': result['data']['id'], 'business_event': 'HistoryTest.delete'})
        return Task.objects.get(id=result['data']['id'])

    def _query(self, arguments, fields='id entityString'):
        response = self.query(
            f'{{ taskHistory({arguments}) {{ edges {{ node {{ {fields} }} }} }} }}',
            headers={'HTTP_AUTHORIZATION': f'Bearer {self.token}'},
        )
        self.assertResponseNoErrors(response)
        return [edge['node'] for edge in json.loads(response.content)['data']['taskHistory']['edges']]

    def test_every_history_row_reports_the_entity_of_its_task(self):
        rows = self._query(f'id: "{self.task.id}"')

        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual({row['entityString'] for row in rows}, {str(self.entity)})

    def test_history_and_task_report_the_same_entity_string(self):
        response = self.query(
            f'{{ task(id: "{self.task.id}") {{ edges {{ node {{ entityString }} }} }} }}',
            headers={'HTTP_AUTHORIZATION': f'Bearer {self.token}'},
        )
        self.assertResponseNoErrors(response)
        task_entity_string = json.loads(response.content)['data']['task']['edges'][0]['node']['entityString']

        rows = self._query(f'id: "{self.task.id}"')

        self.assertEqual({row['entityString'] for row in rows}, {task_entity_string})

    def test_a_history_row_without_entity_reports_null(self):
        rows = self._query(f'id: "{self.task_without_entity.id}"')

        self.assertTrue(rows)
        self.assertEqual({row['entityString'] for row in rows}, {None})

    def test_a_history_row_whose_entity_row_is_missing_reports_null(self):
        Task.history.filter(id=self.task.id).update(entity_id='-1')

        rows = self._query(f'id: "{self.task.id}"')

        self.assertTrue(rows)
        self.assertEqual({row['entityString'] for row in rows}, {None})

    def test_history_is_filtered_on_the_entity_text(self):
        needle = str(self.entity)[-6:].upper()

        rows = self._query(f'entityString_Icontains: "{needle}", source: "history_entity_source"')

        self.assertTrue(rows)
        self.assertTrue(all(needle.lower() in row['entityString'].lower() for row in rows))
        self.assertEqual(
            len(rows), Task.history.filter(id=self.task.id).count(),
            "only the history rows of the task whose entity matches",
        )
