import copy
import datetime
import decimal
import logging
import uuid
from abc import abstractmethod, ABC
from typing import Dict, Type
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from core.datetimes.ad_datetime import AdDate, AdDatetime
from core.forms import User
from core.services import BaseService
from core.signals import register_service_signal
from core.services.utils import check_authentication, output_exception, output_result_success, model_representation
from core.utils import to_json_safe_value
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import TaskGroup, TaskExecutor, Task
from tasks_management.validation import TaskGroupValidation, TaskExecutorValidation, TaskValidation

logger = logging.getLogger(__name__)


class TaskCompletionRefused(Exception):
    """
    Raised through TaskService.resolve_task when the completion that the resolution triggers fails.
    `result` is the failed TaskService.complete_task output. resolve_task runs in one transaction,
    so the refusal also rolls back the business_status and additional data it stored.
    """

    def __init__(self, result):
        self.result = result
        super().__init__(result.get('detail') or result.get('message'))


class TaskService(BaseService):
    OBJECT_TYPE = Task

    def __init__(self, user, validation_class=TaskValidation):
        super().__init__(user, validation_class)

    @transaction.atomic
    @register_service_signal('task_service.create')
    def create(self, obj_data):
        source = obj_data.get('source')
        task_group_query = TaskGroup.objects.filter(json_ext__contains={"task_sources": [source]})
        if task_group_query:
            obj_data = {**obj_data, "task_group": task_group_query.first(), "status": Task.Status.ACCEPTED}
        return super().create(obj_data)

    @register_service_signal('task_service.update')
    def update(self, obj_data):
        return super().update(obj_data)

    @register_service_signal('task_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)

    @register_service_signal('task_service.complete_task')
    def complete_task(self, obj_data):
        try:
            with transaction.atomic():
                obj = self.OBJECT_TYPE.objects.get(id=obj_data['id'])
                obj.status = Task.Status.FAILED if obj_data.get('failed', False) else Task.Status.COMPLETED
                obj.save(username=self.user.login_name)
                return output_result_success({'task': model_representation(obj), 'user': {'id': f"{self.user.id}"}})
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="complete", exception=exc)

    @transaction.atomic
    @register_service_signal('task_service.resolve_task')
    def resolve_task(self, obj_data):
        try:
            self.validation_class.validate_update(self.user, **obj_data)
            obj = self.OBJECT_TYPE.objects.get(id=obj_data['id'])
            incoming_status = obj_data.get('business_status')
            additional_data = obj_data.get('additional_data')
            self._update_task_business_status(obj, incoming_status, additional_data)
            self._insert_additional_data_to_json_ext(obj, additional_data)
            return output_result_success({'task': model_representation(obj), 'user': {'id': f"{self.user.id}"}})
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="resolve", exception=exc)

    def _update_task_business_status(self, task, incoming_status, additional_data):
        task.business_status = self.__deep_merge(task.business_status, incoming_status)
        self._insert_additional_data_to_json_ext(task, additional_data)
        task.save(username=self.user.login_name)

    def _insert_additional_data_to_json_ext(self, obj, additional_data):
        if not additional_data:
            return

        obj.json_ext = obj.json_ext or {}

        existing_additional_data = obj.json_ext.get("additional_resolve_data", {})
        existing_additional_data[str(self.user.id)] = additional_data

        obj.json_ext["additional_resolve_data"] = existing_additional_data

    def __deep_merge(self, dict1, dict2):
        """
        Merges two dictionaries, deeply combining them.
        """
        result = copy.deepcopy(dict1)

        for key, value in dict2.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self.__deep_merge(result[key], value)
            elif key in result and isinstance(result[key], list) and isinstance(value, list):
                result[key] = result[key] + value
            else:
                result[key] = copy.deepcopy(value)

        return result


class TaskGroupService(BaseService):
    OBJECT_TYPE: Type[TaskGroup] = TaskGroup

    def __init__(self, user, validation_class=TaskGroupValidation):
        super().__init__(user, validation_class)

    @check_authentication
    def create(self, obj_data: Dict[str, any]):
        try:
            with transaction.atomic():
                user_ids = obj_data.pop('user_ids')
                obj_data = self._adjust_update_payload(obj_data)
                self.validation_class.validate_create(self.user, **obj_data)
                task_sources = obj_data.pop('task_sources')
                obj_data = {**obj_data, "json_ext": {"task_sources": list(task_sources)}}
                obj_: TaskGroup = self.OBJECT_TYPE(**obj_data)
                task_group_output = self.save_instance(obj_)
                task_group_id = task_group_output['data']['id']
                task_executor_service = TaskExecutorService(self.user)
                # TODO: it would be good to override bulk_create and use it here
                for user_id in user_ids:
                    task_executor_service.create({
                        "task_group_id": task_group_id,
                        "user_id": user_id
                    })
                return task_group_output
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

    @check_authentication
    def update(self, obj_data: Dict[str, any]):
        try:
            with transaction.atomic():
                user_ids = obj_data.pop('user_ids')
                obj_data = self._adjust_update_payload(obj_data)
                self.validation_class.validate_update(self.user, **obj_data)
                task_sources = obj_data.pop('task_sources')
                task_group_id = obj_data.get('id')
                task_group = TaskGroup.objects.get(id=task_group_id)
                json_ext = task_group.json_ext if task_group.json_ext else dict()
                obj_data = {**obj_data, "json_ext": {**json_ext, "task_sources": list(task_sources)}}
                current_task_executors = task_group.taskexecutor_set.filter(is_deleted=False)
                current_user_ids = current_task_executors.values_list('user__id', flat=True)
                if set(current_user_ids) != set(user_ids):
                    self._update_task_group_task_executors(task_group, user_ids)
                return super().update(obj_data)
        except Exception as exc:
            import traceback
            logger.debug(traceback.format_exc())
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

    @transaction.atomic
    def _update_task_group_task_executors(self, task_group, user_ids):
        try:
            task_group.taskexecutor_set.all().delete()
            service = TaskExecutorService(self.user)
            for user_id in user_ids:
                service.create({'task_group_id': task_group.id,
                                'user_id': user_id})
        except Exception as exc:
            raise exc

    def delete(self, obj_data: Dict[str, any]):
        id = obj_data.get("id")
        if id:
            task_group = TaskGroup.objects.filter(id=id).first()
            task_group.taskexecutor_set.all().delete()
        return super().delete(obj_data)

    def _base_payload_adjust(self, obj_data):
        task_sources = obj_data.pop('task_sources', [])
        if task_sources:
            task_sources = set(task_sources)
        return {**obj_data, 'task_sources': task_sources}


class TaskExecutorService(BaseService):
    OBJECT_TYPE: Type[TaskExecutor] = TaskExecutor

    def __init__(self, user, validation_class=TaskExecutorValidation):
        super().__init__(user, validation_class)


class CreateCheckerLogicServiceMixin(ABC):
    """
    Provides default implementation for creating a create task for maker-checker logic.
    To be used in implementations of core.services.BaseService.
    """

    @property
    @abstractmethod
    def OBJECT_TYPE(self):
        pass

    def create_create_task(self, obj_data):
        try:
            with transaction.atomic():
                self.validation_class.validate_create(self.user, **obj_data)
                task_service = TaskService(self.user)
                task_data = {
                    'source': self._create_source,
                    'business_data_serializer': self._get_business_data_serializer(),
                    'executor_action_event': self._create_executor_event,
                    'business_event': self._create_business_event,
                    'data': self._adjust_create_task_data(None, copy.deepcopy(obj_data)),
                }
                return task_service.create(task_data)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create_create_task", exception=exc)

    @property
    def _create_source(self):
        return self.__class__.__name__

    @property
    def _create_business_event(self):
        return f'{self.__class__.__name__}.create'

    @property
    def _create_executor_event(self):
        return TasksManagementConfig.default_executor_event

    def _adjust_create_task_data(self, entity, obj_data):
        return _get_std_crud_task_data_payload(entity, obj_data)

    def _get_business_data_serializer(self):
        return f'{self.__class__.__module__}.{self.__class__.__name__}._business_data_serializer'

    def _business_data_serializer(self, data):
        return data


class UpdateCheckerLogicServiceMixin(ABC):
    """
    Provides default implementation for creating an update task for maker-checker logic.
    To be used in implementations of core.services.BaseService.
    """

    @property
    @abstractmethod
    def OBJECT_TYPE(self):
        pass

    def create_update_task(self, obj_data):
        try:
            with transaction.atomic():
                self.validation_class.validate_update(self.user, **obj_data)
                task_service = TaskService(self.user)
                obj = self.OBJECT_TYPE.objects.get(id=obj_data['id'])
                task_data = {
                    'source': self._update_source,
                    'business_data_serializer': self._get_business_data_serializer(),
                    'entity_id': obj.id,
                    'entity_type': ContentType.objects.get_for_model(self.OBJECT_TYPE),
                    'executor_action_event': self._update_executor_event,
                    'business_event': self._update_business_event,
                    'data': self._adjust_update_task_data(obj, copy.deepcopy(obj_data)),
                }
                return task_service.create(task_data)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create_update_task", exception=exc)

    @property
    def _update_source(self):
        return self.__class__.__name__

    @property
    def _update_business_event(self):
        return f'{self.__class__.__name__}.update'

    @property
    def _update_executor_event(self):
        return TasksManagementConfig.default_executor_event

    def _adjust_update_task_data(self, entity, obj_data):
        self._align_json_ext(obj_data)
        return _get_std_crud_task_data_payload(entity, obj_data)

    def _align_json_ext(self, obj_data):
        json_ext = obj_data.get('json_ext')
        if isinstance(json_ext, dict):
            for key, value in obj_data.items():
                if key in json_ext and json_ext[key] != value:
                    json_ext[key] = to_json_safe_value(value)

    def _get_business_data_serializer(self):
        return f'{self.__class__.__module__}.{self.__class__.__name__}._business_data_serializer'

    def _business_data_serializer(self, data):
        return data


class DeleteCheckerLogicServiceMixin(ABC):
    """
    Provides default implementation for creating a delete task for maker-checker logic.
    To be used in implementations of core.services.BaseService.
    """

    @property
    @abstractmethod
    def OBJECT_TYPE(self):
        pass

    def create_delete_task(self, obj_data):
        try:
            with transaction.atomic():
                self.validation_class.validate_delete(self.user, **obj_data)
                task_service = TaskService(self.user)
                obj = self.OBJECT_TYPE.objects.get(id=obj_data['id'])
                task_data = {
                    'source': self._delete_source,
                    'business_data_serializer': self._get_business_data_serializer(),
                    'entity_id': obj.id,
                    'entity_type': ContentType.objects.get_for_model(self.OBJECT_TYPE),
                    'executor_action_event': self._delete_executor_event,
                    'business_event': self._delete_business_event,
                    'data': self._adjust_delete_task_data(None, copy.deepcopy(obj_data)),
                }
                return task_service.create(task_data)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create_delete_task", exception=exc)

    @property
    def _delete_source(self):
        return self.__class__.__name__

    @property
    def _delete_business_event(self):
        return f'{self.__class__.__name__}.delete'

    @property
    def _delete_executor_event(self):
        return TasksManagementConfig.default_executor_event

    def _adjust_delete_task_data(self, entity, obj_data):
        return _get_std_crud_task_data_payload(entity, obj_data)

    def _get_business_data_serializer(self):
        return f'{self.__class__.__module__}.{self.__class__.__name__}._business_data_serializer'

    def _business_data_serializer(self, data):
        return data


class CheckerLogicServiceMixin(CreateCheckerLogicServiceMixin,
                               UpdateCheckerLogicServiceMixin,
                               DeleteCheckerLogicServiceMixin,
                               ABC):
    """
    Provides default implementation for creating "create", "update", and "delete" tasks for maker-checker logic
    To be used in implementations of core.services.BaseService.
    """
    pass


def on_task_complete_service_handler(service_type):
    """
    Generic complete_task handler any combination of CreateCheckerLogicServiceMixin,
    UpdateCheckerLogicServiceMixin, DeleteCheckerLogicServiceMixin. It will automatically detect available
    task business events fot that service type.

    :param service_type: BaseService subclass implementing any <Operation>CheckerLogicServiceMixin
    :return: event handler that will be able to execute task
    """
    operations = []
    if issubclass(service_type, CreateCheckerLogicServiceMixin):
        operations.append('create')
    if issubclass(service_type, UpdateCheckerLogicServiceMixin):
        operations.append('update')
    if issubclass(service_type, DeleteCheckerLogicServiceMixin):
        operations.append('delete')

    def service_operation_handler(operation, user, data):
        # Run the operation form a service by name
        # getattr(ExampleService(user), 'update')(data)
        return getattr(service_type(user), operation)(data)

    def func(**kwargs):
        try:
            result = kwargs.get('result', {})
            task = result['data']['task']
            business_event = task['business_event']
            # Tasks generated with CheckerLogicServiceMixin use naming scheme `ServiceName.operation` as business event.
            # Checking if the task was generated by the mixin and if the service provided for the handler match
            service_match = business_event.startswith(f"{service_type.__name__}.")
            if result and result['success'] \
                    and task['status'] == Task.Status.COMPLETED \
                    and service_match:
                # Extracting `operation` part from `ServiceName.operation`
                operation = business_event.split(".")[1]
                if operation in operations:
                    user = User.objects.get(id=result['data']['user']['id'])
                    data = task['data']['incoming_data']
                    service_operation_handler(operation, user, data)
        except Exception as e:
            logger.error("Error while executing on_task_complete", exc_info=e)
            return [str(e)]

    return func


def _get_std_crud_task_data_payload(entity, payload):
    incoming_data = {}
    current_data = {}

    for key in payload:
        incoming_value = to_json_safe_value(payload[key])
        incoming_data[key] = incoming_value

        if entity:
            entity_value = getattr(entity, key)
            current_data[key] = to_json_safe_value(entity_value) if entity_value else entity_value

    return {"incoming_data": incoming_data, "current_data": current_data}


def _get_std_task_data_payload(payload):
    incoming_data = {}

    for key in payload:
        incoming_value = to_json_safe_value(payload[key])
        incoming_data[key] = incoming_value

    return incoming_data


def crud_business_data_builder(data, serializer):
    serialized_data = copy.deepcopy(data)
    for data_key, data_value in data.items():
        serialized_data[data_key] = {
            key: serializer(key, value) for key, value in data_value.items()
        }
    return serialized_data
