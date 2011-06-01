# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4
import uuid

from zope.interface import classProvides
from twisted.python import failure

from feat.common import log, defer, fiber, observer, time
from feat.common import serialization, error_handler
from feat.agents.base import replay

from feat.interface.protocols import *
from feat.interface.serialization import *


class CancelFiber(Exception):
    pass


class StateAssertationError(RuntimeError):
    pass


class StateMachineMixin(object):
    '''
    Mixin used by numerous objects. Defines the state and provides utilities
    for making decisions based on state.
    '''

    _notifier = None

    def __init__(self, state=None):
        self.state = state
        self._notifier = defer.Notifier()

    @serialization.freeze_tag('StateMachineMixin.wait_for_state')
    def wait_for_state(self, *states):
        if self.state in states:
            return defer.succeed(self)
        d = defer.DeferredList(
            map(lambda state: self._notifier.wait(state), states),
            fireOnOneCallback=True)
        d.addCallback(lambda _: self)
        return d

    def _set_state(self, state):
        if not self.state or not (state == self.state):
            self.log('Changing state from %r to %r', self.state, state)
            self.state = state

        if self._notifier:
            self._notifier.callback(state, self)

    def _cmp_state(self, states):
        if not isinstance(states, (list, tuple, )):
            states = [states]
        return self.state in states

    def _ensure_state(self, states):
        if self._cmp_state(states):
            return True
        raise StateAssertationError("Expected state in: %r, was: %r instead" %\
                           (states, self.state))

    def _get_machine_state(self):
        return self.state

    def _event_handler(self, mapping, event):
        klass = event.__class__
        decision = mapping.get(klass, None)
        if not decision:
            self.warning("Unknown event received %r. Ignoring", event)
            return False

        if isinstance(decision, list):
            match = filter(
                lambda x: self._cmp_state(x['state_before']), decision)
            if len(match) != 1:
                self.warning("Expected to find excatly one handler for %r in "
                             "state %r, found %r handlers", event,
                             self.get_machine_state(),
                             len(match))
                return False
            decision = match[0]

        state_before = decision['state_before']
        try:
            self._ensure_state(state_before)
        except StateAssertationError:
            self.warning("Received event: %r in state: %r, expected state "
                         "for this method is: %r",
                         klass, self._get_machine_state(),
                         decision['state_before'])
            return False

        state_after = decision['state_after']
        self._set_state(state_after)

        self._call(decision['method'], event)

    # Make it possible to use mixin without the logging submodule

    def log(self, *args):
        if isinstance(self, log.Logger):
            log.Logger.log(self, *args)

    def debug(self, *args):
        if isinstance(self, log.Logger):
            log.Logger.debug(self, *args)

    def info(self, *args):
        if isinstance(self, log.Logger):
            log.Logger.info(self, *args)

    def warning(self, *args):
        if isinstance(self, log.Logger):
            log.Logger.warning(self, *args)

    def error(self, *args):
        if isinstance(self, log.Logger):
            log.Logger.error(self, *args)


class AgencyMiddleMixin(object):
    '''Responsible for formating messages, calling methods etc'''

    guid = None

    protocol_id = None
    remote_id = None

    error_state = None

    def __init__(self, remote_id=None, protocol_id=None):
        self.guid = str(uuid.uuid1())
        self._set_remote_id(remote_id)
        self._set_protocol_id(protocol_id)

    def is_idle(self):
        return False

    def _set_remote_id(self, remote_id):
        if self.remote_id is not None and self.remote_id != remote_id:
            self.debug('Changing id of remote peer. %r -> %r. '
                       'This usually means the message has been handed over.',
                       self.remote_id, remote_id)
        self.remote_id = remote_id

    def _set_protocol_id(self, protocol_id):
        self.protocol_id = protocol_id

    def _send_message(self, msg, expiration_time=None, recipients=None,
                      remote_id=None):
        msg.sender_id = self.guid
        msg.receiver_id = remote_id or self.remote_id
        msg.protocol_id = self.protocol_id
        if msg.expiration_time is None:
            if expiration_time is None:
                expiration_time = time.future(10)
            msg.expiration_time = expiration_time

        if not recipients:
            recipients = self.recipients

        return self.agent.send_msg(recipients, msg)

    def _handover_message(self, msg, remote_id=None):
        msg.receiver_id = remote_id or self.remote_id
        return self.agent.send_msg(self.recipients, msg, handover=True)

    def _call(self, method, *args, **kwargs):
        '''Call the method, wrap it in Deferred and bind error handler'''
        d = defer.maybeDeferred(method, *args, **kwargs)
        d.addErrback(self._error_handler)
        return d

    def _error_handler(self, f):
        if f.check(CancelFiber):
            self.debug('Swallowing CancelFiber exception. This means that the'
                       ' ensure_state() call detected incorrect state and '
                       'fiber was terminated.')
            return

        error_handler(self, f)
        self._set_state(self.error_state)
        self._terminate(f)

    @serialization.freeze_tag('IAgencyStatefulProtocol.ensure_state')
    def ensure_state(self, states):
        '''
        Exposed in a public interface. Use this to mark a point in the fiber
        where it should get cancelled if the state machine is not in expected
        state.
        '''
        try:
            self._ensure_state(states)
        except StateAssertationError:
            raise CancelFiber()


class ExpirationCallsMixin(object):
    '''
    Mixin class used by protocol peers for protecting execution time with
    timeout.
    '''

    def __init__(self):
        self._expiration_call = None

    @replay.side_effect
    def get_expiration_time(self):
        if self._expiration_call:
            return self._expiration_call.getTime()

    def _get_time(self):
        raise NotImplemented('Should be define in the class using the mixin')

    def _setup_expiration_call(self, expire_time, state,
                               method, *args, **kwargs):
        self.log('Setting expiration call of method: %r.%r',
                 self.__class__.__name__, method.__name__)

        time_left = time.left(expire_time)
        if time_left < 0:
            raise RuntimeError('Tried to call method in the past! ETA: %r' %
                               (time_left, ))

        def to_call(callback):
            if state:
                self._set_state(state)
            self.log('Calling method: %r with args: %r', method, args)
            d = defer.maybeDeferred(method, *args, **kwargs)
            d.addErrback(self._error_handler)
            d.addCallback(callback.callback)

        result = defer.Deferred()
        self._expiration_call = time.callLater(
            time_left, to_call, result)
        return result

    def _expire_at(self, expire_time, state, method, *args, **kwargs):
        d = self._setup_expiration_call(expire_time, state,
                                        method, *args, **kwargs)
        d.addCallback(lambda _: self._terminate(ProtocolExpired(_)))
        return d

    @replay.side_effect
    def _cancel_expiration_call(self):
        if self._expiration_call and self._expiration_call.active():
            self.log('Canceling expiration call')
            self._expiration_call.cancel()
            self._expiration_call = None

    def _terminate(self):
        self._cancel_expiration_call()

    def expire_now(self):
        if self._expiration_call and self._expiration_call.active():
            self._expiration_call.reset(0)
            d = self.notify_finish()
            return d
        self.error('Expiration call %r is None or was already called '
                   'or cancelled', self._expiration_call)
        return defer.fail(ProtocolExpired())


class InitiatorMediumBase(object):

    def _terminate(self):
        '''Nothing special.'''


class TransientInitiatorMediumBase(InitiatorMediumBase):

    def __init__(self):
        self._fnotifier = defer.Notifier()

    @serialization.freeze_tag('IAgencyProtocol.notify_finish')
    def notify_finish(self):
        return self._fnotifier.wait('finish')

    def _terminate(self, result):
        if isinstance(result, (failure.Failure, Exception)):
            self.log("Firing errback of notifier with result: %r.", result)
            self.call_next(self._fnotifier.errback, 'finish', result)
        else:
            self.log("Firing callback of notifier with result: %r.", result)
            self.call_next(self._fnotifier.callback, 'finish', result)


class InterestedMediumBase(object):

    def _terminate(self):
        '''Nothing special.'''


class TransientInterestedMediumBase(InterestedMediumBase):

    def __init__(self):
        self._fnotifier = defer.Notifier()

    def _terminate(self, result):
        self.call_next(self._fnotifier.callback, 'finish', result)

    @serialization.freeze_tag('IAgencyProtocol.notify_finish')
    def notify_finish(self):
        return self._fnotifier.wait('finish')

    def call_next(self, *_):
        raise NotImplementedError("This method should be implemented outside "
                                  "of this mixin!")


@serialization.register
class Observer(observer.Observer):
    classProvides(IRestorator)

    active = replay.side_effect(observer.Observer.active)
    get_result = replay.side_effect(observer.Observer.get_result)
