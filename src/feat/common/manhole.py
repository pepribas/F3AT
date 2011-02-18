# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4
import re
import copy
from functools import partial

from twisted.internet import defer
from twisted.spread import pb

from feat.common import decorator, annotate, enum, log, error_handler


class SecurityLevel(enum.Enum):
    """
    safe - should be used to expose querying commands which
           will not mess up with the state
    unsafe - should be for the operations which require a bit of thinking
    superhuman - should not be used, but does it mean we shouldn't have it?
    """

    (safe, unsafe, superhuman, ) = range(3)


@decorator.parametrized_function
def expose(function, security_level=SecurityLevel.safe):
    annotate.injectClassCallback("recorded", 4,
                                 "_register_exposed", function,
                                 security_level)

    return function


class Manhole(annotate.Annotable, pb.Referenceable):

    @classmethod
    def __class__init__(cls, name, bases, dct):
        cls._exposed = dict()
        for base in bases:
            base_exposed = getattr(base, '_exposed', dict())
            cls._exposed.update(copy.deepcopy(base_exposed))

    @classmethod
    def _register_exposed(cls, function, security_level):
        for lvl in SecurityLevel:
            if lvl > security_level:
                continue
            fun_id = function.__name__
            if lvl not in cls._exposed:

                cls._exposed[lvl] = dict()
            cls._exposed[lvl][fun_id] = function
        cls._build_remote_call(function)

    @classmethod
    def _build_remote_call(cls, function):
        f_name = "remote_%s" % function.__name__

        def wrapped(*args, **kwargs):
            res = function(*args, **kwargs)
            if isinstance(res, pb.Referenceable):
                return res
            else:
                #TODO: Serialize it
                return res

        wrapped.__name__ = f_name
        setattr(cls, f_name, wrapped)

    @expose()
    def help(self):
        '''help() -> Prints exposed methods and their docstrings.'''
        cmds = self.get_exposed_cmds()
        return "\n".join([self._format_help(x) for x in cmds.values()])

    def _format_help(self, method):
        return "%s %s" % (method.__name__.ljust(25), method.__doc__)

    def get_exposed_cmds(self, lvl=SecurityLevel.safe):
        if self._exposed is None or lvl not in self._exposed:
            return dict()
        else:
            return self._exposed[lvl]

    def remote_get_exposed_cmds(self, lvl=SecurityLevel.safe):
        return self.get_exposed_cmds(lvl).keys()

    def lookup_cmd(self, name, lvl=SecurityLevel.safe):
        commands = self.get_exposed_cmds(lvl)
        if name not in commands:
            raise UnknownCommand('Unknown command: %s.%s' %\
                                 (self.__class__.__name__, name, ))
        return partial(commands[name], self)


class PBRemote(object):

    def __init__(self, obj):
        self.obj = obj
        # names of exposed commands
        self.commands = list()

    def initiate(self):
        d = self.obj.callRemote('get_exposed_cmds')
        d.addCallback(self._set_cmds)
        return d

    def _set_cmds(self, cmds):
        self.commands = cmds

    def lookup_cmd(self, name, lvl=SecurityLevel.safe):
        if name not in self.commands:
            raise UnknownCommand('Unknown command: %s.%s' %\
                                 (self.__class__.__name__, name, ))
        return partial(self.obj.callRemote, name)


class Parser(log.Logger):

    log_category = 'command-parser'

    def __init__(self, driver, output, commands, cb_on_finish=None):
        log.Logger.__init__(self, driver)

        self.cb_on_finish = cb_on_finish
        self.commands = commands
        self.buffer = ""
        self.output = output
        self._locals = dict()
        self._last_line = None
        self.re = dict(
            assignment=re.compile('\A(\w+)\s*=\s*(\S.*)'),
            async=re.compile('\Aasync\s+(.+)'),
            yielding=re.compile('\Ayield\s+(\w+)\s*\Z'),
            number=re.compile('\A\d+(\.\d+)?\Z'),
            none=re.compile('\ANone\Z'),
            true=re.compile('\ATrue\Z'),
            false=re.compile('\AFalse\Z'),
            string=re.compile('\A\'([^(?<!\)\']*)\'\Z'),
            call=re.compile('\A(\w+)\((.*)\)\Z'),
            variable=re.compile('\A([^\(\)\'\"\s\+]+)\Z'),
            method_call=re.compile('\A(\w+)\.(\w+)\((.*)\)\Z'))

    def split(self, text):
        '''
        Splits the text with function arguments into the array with first
        class citizens separated. See the unit tests for clarificatin.
        '''
        # nesting character -> count
        counters = {"'": False, '(': 0}

        def reverse(char):

            def wrapped():
                self.debug('Reverse %s', char)
                counters[char] = not counters[char]
            return wrapped

        def increase(char):

            def wrapped():
                self.debug('Increase %s', char)
                counters[char] += 1
            return wrapped

        def decrease(char):

            def wrapped():
                self.debug('Decrease %s', char)
                counters[char] -= 1
            return wrapped

        def is_top_level():
            return all([not x for x in counters.values()])

        def fail():
            raise BadSyntax('Syntax error processing line: %s' %\
                            self._last_line)


        temp = ""
        # end of field flag indicates that we expect next character to be
        # either whitespace of split
        eof_flag = False

        def append_char(temp, char):
            if eof_flag:
                fail()
            temp += char
            return temp

        # dictionary char -> handler
        nesters = {"'": reverse("'"), "(": increase('('), ")": decrease('(')}
        # chars to split on
        split = (',', )
        # chars to swallow
        consume = (' ', '\n')

        result = list()

        self.debug("spliting: %s", text)

        for char in text:
            self.debug('Char: %s. counters: %r', char, counters)
            if char in nesters:
                self.debug('Nesting: %s', char)
                nesters[char]()
                self.info('COunters: %r', counters)
                temp = append_char(temp, char)
            elif not is_top_level():
                self.debug('not on top')
                temp = append_char(temp, char)
            elif char in consume:
                if len(temp) > 0:
                    eof_flag = True
                continue
            elif char in split:
                self.debug('Appending: %s', temp)
                result.append(temp)
                temp = ""
                eof_flag = False
            else:
                temp = append_char(temp, char)
        if len(temp) > 0:
            result.append(temp)

        if not is_top_level():
            fail()
        self.info('Split returns %r', result)
        return result

    def dataReceived(self, data):
        self.buffer += data
        self.process_line()

    def send_output(self, data):
        if data is not None:
            self.output.write(str(data) + "\n")

    def get_line(self):
        try:
            index = self.buffer.index("\n")
            line = self.buffer[0:index]
            self.buffer = self.buffer[(index + 1):]
            return line
        except ValueError:
            return None

    def process_line(self):
        line = self.get_line()
        if line is not None:
            self._last_line = line
            self.debug('Processing line: %s', line)
            if not re.search('\w', line):
                self.log('Empty line')
                return self.process_line()

            assignment = self.re['assignment'].search(line)
            if assignment:
                variable_name = assignment.group(1)
                line = assignment.group(2)

            async = self.re['async'].search(line)
            if async:
                line = async.group(1)
                async = True

            yielding = self.re['yielding'].search(line)
            if yielding:
                varname = yielding.group(1)
                d = defer.succeed(varname)
                d.addCallback(self.get_local)
                d.addCallback(WrappedDeferred.get_defer)
            else: #normal processing
                d = defer.maybeDeferred(self.split, line)
                d.addCallback(self.process_array, async)
                d.addCallback(self.validate_result)

            if assignment:
                d.addCallback(self.set_local, variable_name)
            d.addCallback(self.set_local, '_')
            d.addCallback(self.send_output)
            d.addCallbacks(lambda _: self.process_line(), self._error_handler)
        else:
            self.on_finish()

    @defer.inlineCallbacks
    def process_array(self, array, async=False):
        """
        Main part of the protocol handling. Whan comes in as the parameter is
        a array of expresions, for example:

        ["1", "'some string'", "variable",
         "some_call(param1, some_other_call())"]

        Each element of the is evaluated in synchronous way. In case of method
        calls, the call is performed by iterating the method.

        The result of the function is list with elements subsituted by the
        values they stand for (for variables: values, for method calls: the
        result of deferred returned).

        The async parametr (default False) tells whether to yield the Deferred
        returned. If False, the Deferreds are substitued with None.
        """

        result = list()
        kwargs = dict()
        keyword = None

        def append_result(value):
            if keyword:
                kwargs[keyword] = value
            else:
                result.append(value)

        for element in array:
            self.log('matching: %s', element)

            # First check for expresion with the form keyword=expresion
            keyword = None
            assignment = self.re['assignment'].search(element)
            if assignment:
                keyword = assignment.group(1)
                element = assignment.group(2)

            m = self.re['number'].search(element)
            if m:
                append_result(eval(m.group(0)))
                continue

            m = self.re['string'].search(element)
            if m:
                append_result(m.group(1))
                continue

            m = self.re['none'].search(element)
            if m:
                append_result(None)
                continue

            m = self.re['true'].search(element)
            if m:
                append_result(True)
                continue

            m = self.re['false'].search(element)
            if m:
                append_result(False)
                continue

            m = self.re['variable'].search(element)
            if m:
                append_result(self.get_local(m.group(1)))
                continue

            m = self.re['call'].search(element)
            n = self.re['method_call'].search(element)
            if m or n:
                if m:
                    command = m.group(1)
                    method = self.commands.lookup_cmd(command)
                    rest = m.group(2)
                else:
                    obj = n.group(1)
                    local = self.get_local(obj)
                    if not isinstance(local, (Manhole, PBRemote, )):
                        raise IllegalCall('Variable %r should be a Manhole '
                                          'instance to make this work! '
                                          'Got %r instead.' %\
                                          (obj, type(local)))
                    command = n.group(2)
                    method = local.lookup_cmd(command)
                    rest = n.group(3)
                arguments, keywords =\
                           yield self.process_array(self.split(rest))
                output = method(*arguments, **keywords)
                if isinstance(output, defer.Deferred):
                    if not async:
                        output = yield output
                    else:
                        output = WrappedDeferred(output)

                if isinstance(output, pb.RemoteReference):
                    output = PBRemote(output)
                    yield output.initiate()

                self.debug("Finished processing command: %s.", element)
                append_result(output)
                continue

            raise BadSyntax('Syntax error processing line: %s. '
                            'Could not detect type of element: %s' %\
                            (self._last_line, element, ))

        defer.returnValue((result, kwargs, ))

    def validate_result(self, (result_array, result_keywords, )):
        '''
        Check that the result is a list with a single element, and return it.
        If we had more than one element it would mean that the line processed
        looked somewhat like this:
        call1(), "blah blah blah"
        '''
        if len(result_array) != 1 or len(result_keywords) > 0:
            raise BadSyntax('Syntax error processing line: %s' %\
                            self._last_line)
        return result_array[0]

    def on_finish(self):
        '''
        Called when there is no more messages to be processed in the buffer.
        '''
        if callable(self.cb_on_finish):
            self.cb_on_finish()

    def set_local(self, value, variable_name):
        '''
        Assign local variable. The line processed looked somewhat like this:
        variable = some_call()
        '''
        self.log('assigning %s = %r', variable_name, value)
        self._locals[variable_name] = value
        return value

    def get_local(self, variable_name):
        '''
        Return the value of the local variable. Raises UnknownVariable is
        the name is not known.
        '''
        if variable_name not in self._locals:
            raise UnknownVariable('Unknown variable %s' % variable_name)
        return self._locals[variable_name]

    def _error_handler(self, f):
        error_handler(self, f)
        self.send_output(f.getErrorMessage())
        self.on_finish()


class BadSyntax(Exception):
    pass


class IllegalCall(Exception):
    pass


class UnknownVariable(Exception):
    pass


class UnknownCommand(Exception):
    pass


class WrappedDeferred(object):

    def __init__(self, d):
        self.d = d

    def get_defer(self):
        return self.d
