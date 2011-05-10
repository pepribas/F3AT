# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4
import warnings

from twisted.python import components, failure
from zope.interface import implements

from feat.agents.base import replay
from feat.agencies import common, protocols
from feat.common import (log, enum, defer, time, error_handler, )

from feat.agencies.interface import *
from feat.interface.serialization import *
from feat.interface.task import *
from feat.interface.protocols import *


class TaskState(enum.Enum):
    '''
    performing - Task is running
    completed - Task is finished
    error - Task has an error
    expired - Task timeout
    '''

    (performing, completed, expired, error) = range(4)


class AgencyTask(log.LogProxy, log.Logger, common.StateMachineMixin,
                 common.ExpirationCallsMixin, common.AgencyMiddleMixin,
                 common.TransientInitiatorMediumBase):

    implements(ISerializable, IAgencyTask, IAgencyProtocolInternal)

    log_category = 'agency-task'

    type_name = 'task-medium'

    idle = False # By default tasks are NOT idle

    _error_handler = error_handler

    def __init__(self, agency_agent, factory, *args, **kwargs):
        log.Logger.__init__(self, agency_agent)
        log.LogProxy.__init__(self, agency_agent)
        common.StateMachineMixin.__init__(self)
        common.ExpirationCallsMixin.__init__(self)
        common.AgencyMiddleMixin.__init__(self)
        common.TransientInitiatorMediumBase.__init__(self)

        self.agent = agency_agent
        self.factory = factory
        self.args = args
        self.kwargs = kwargs

    ### IAgencyTask Methods ###

    def initiate(self):
        self.agent.journal_protocol_created(self.factory, self,
                                            *self.args, **self.kwargs)
        task = self.factory(self.agent.get_agent(), self)
        self.agent.register_protocol(self)

        self.task = task
        self.log_name = self.task.__class__.__name__

        self._set_state(TaskState.performing)

        self._cancel_expiration_call()

        if self.task.timeout:
            timeout = time.future(self.task.timeout)
            error = ProtocolExpired("Timeout exceeded waiting "
                                     "for task.initate()")
            self._expire_at(timeout, TaskState.expired,
                            self._expired, failure.Failure(error))

        self.call_next(self._initiate, *self.args, **self.kwargs)

        return task

    ### IAgencyProtocolInternal Methods ###

    def is_idle(self):
        return self.idle

    def get_agent_side(self):
        return self.task

    def cleanup(self):
        if self.timeout:
            return common.ExpirationCallsMixin.cleanup()
        #FIXME: calling expired anyway when no timeout is not the way
        return self._call(self.task.expired)

    # notify_finish() implemented in common.TransientInitiatorMediumBase

    @replay.named_side_effect('AgencyTask.terminate')
    def finish(self, arg=None):
        warnings.warn("AgencyTask.finish() is deprecated, "
                      "please use AgencyTask.terminate()",
                      DeprecationWarning, stacklevel=2)
        self._completed(arg)

    @replay.named_side_effect('AgencyTask.terminate')
    def terminate(self, arg=None):
        self._completed(arg)

    @replay.named_side_effect('AgencyTask.fail')
    def fail(self, fail):
        if isinstance(fail, Exception):
            fail = failure.Failure(fail)
        self._error(fail)

    @replay.named_side_effect('AgencyTask.finished')
    def finished(self):
        return not self._cmp_state(TaskState.performing)

    ### ISerializable Methods ###

    def snapshot(self):
        return id(self)

    ### Required by InitiatorMediumbase ###

    def call_next(self, _method, *args, **kwargs):
        return self.agent.call_next(_method, *args, **kwargs)

    # Used by ExpirationCallsMixin

    def _get_time(self):
        return self.agent.get_time()

    ### Private Methods ###

    def _initiate(self, *args, **kwargs):
        d = defer.maybeDeferred(self.task.initiate, *args, **kwargs)
        d.addCallbacks(self._completed, self._error)
        return d

    def _completed(self, arg):
        if arg != NOT_DONE_YET or not self._cmp_state(TaskState.performing):
            self._set_state(TaskState.completed)
            time.callLater(0, self._terminate, arg)

    def _error(self, arg):
        self._error_handler(arg)
        self._set_state(TaskState.error)
        time.callLater(0, self._terminate, arg)

    def _expired(self, arg):
        self._set_state(TaskState.expired)
        d = defer.maybeDeferred(self.task.expired)
        return d

    def _terminate(self, result):
        common.ExpirationCallsMixin._terminate(self)

        self.log("Unregistering task %s" % self.guid)
        self.agent.unregister_protocol(self)

        common.TransientInitiatorMediumBase._terminate(self, result)
        return defer.succeed(self)


class AgencyTaskFactory(protocols.BaseInitiatorFactory):
    type_name = 'task-medium-factory'
    protocol_factory = AgencyTask

    def __call__(self, agency_agent, *args, **kwargs):
        # Dropping recipients
        return self.protocol_factory(agency_agent, self._factory,
                                     *args, **kwargs)


components.registerAdapter(AgencyTaskFactory,
                           ITaskFactory,
                           IAgencyInitiatorFactory)
