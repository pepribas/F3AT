from twisted.internet import defer, reactor
from twisted.trial import unittest

from feat.common import log

log.FluLogKeeper.init('test.log')


class TestCase(unittest.TestCase, log.FluLogKeeper, log.Logger):

    log_category = "test"

    def __init__(self, *args, **kwargs):
        unittest.TestCase.__init__(self, *args, **kwargs)
        log.FluLogKeeper.__init__(self)
        log.Logger.__init__(self, self)


    def cb_after(self, arg, obj, method):
        '''
        Returns defered fired after the call of method on object.
        Can be used in defered chain like this:

        d.addCallback(doSomeStuff)
        d.addCallback(self._cb_after, obj=something, method=some_method)
        d.addCallback(jobAfterCallOfSomeMethod)

        This will fire last callback after something.some_method has been
        called.
        Parameter passed to the last callback is either return value of
        doSomeStuff, or, if this is None, the return value of stubbed method.
        '''
        old_method = obj.__getattribute__(method)
        d = defer.Deferred()

        def new_method(*args, **kwargs):
            obj.__setattr__(method, old_method)
            ret = old_method(*args, **kwargs)
            reactor.callLater(0, d.callback, arg or ret)
            return ret

        obj.__setattr__(method, new_method)

        return d

    def assertCalled(self, obj, name, times=1):
        assert isinstance(obj, Mock)
        times_called = len(obj.find_calls(name))
        template = "Expected %s method to be called %d time(s), "\
                   "was called %d time(s)"
        self.assertEqual(times, times_called,\
                             template % (name, times, times_called))
        

def stub(method):

    def decorated(self, *args, **kwargs):
        call = MockCall(method.__name__, args, kwargs)
        self._called.append(call)

    return decorated


class Mock(object):
    
    def __init__(self):
        self._called = []

    def find_calls(self, name):
        return filter(lambda x: x.name == name, self._called)


class MockCall(object):
    
    def __init__(self, name, args, kwargs):
        self.name = name
        self.args = args
        self.kwargs = kwargs