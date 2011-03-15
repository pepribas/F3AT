from zope.interface import directlyProvides, Interface

from feat.interface.agency import ExecMode
from feat.agents.base import (testsuite, agent, dependency, descriptor, )


class TestDependency(testsuite.TestCase):

    def setUp(self):
        testsuite.TestCase.setUp(self)
        instance = self.ball.generate_agent(AgentWithDependency)
        self.agent = self.ball.load(instance)

    def testCallDependency(self):
        expected = [
            testsuite.side_effect('AgencyAgent.get_mode',
                                  ExecMode.test, (SomeInterface, ))]
        out, _ = self.ball.call(expected, self.agent.dependency, SomeInterface)
        self.assertEqual(out, ExecMode.test)


        expected = [
            testsuite.side_effect('AgencyAgent.get_mode',
                                  ExecMode.production, (SomeInterface, ))]
        out, _ = self.ball.call(expected, self.agent.dependency, SomeInterface)
        self.assertEqual(out, ExecMode.production)

    def testCallUnknown(self):
        expected = [
            testsuite.side_effect('AgencyAgent.get_mode',
                                  ExecMode.test, (UnknownInterface, ))]
        self.assertRaises(dependency.UndefinedDependency, self.ball.call,
                          expected, self.agent.dependency, UnknownInterface)

    def testCallUndefined(self):
        expected = [
            testsuite.side_effect('AgencyAgent.get_mode',
                                  ExecMode.simulation, (SomeInterface, ))]
        self.assertRaises(dependency.UndefinedDependency, self.ball.call,
                          expected, self.agent.dependency, SomeInterface)


class SomeInterface(Interface):

    def __call__():
        pass


class UnknownInterface(Interface):

    def __call__():
        pass


def test():
    return ExecMode.test
directlyProvides(test, SomeInterface)


def production():
    return ExecMode.production
directlyProvides(production, SomeInterface)


@agent.register('blah_blah')
class AgentWithDependency(agent.BaseAgent):

    dependency.register(
        SomeInterface, 'feat.test.test_agents_base_dependency.test',
        ExecMode.test)
    dependency.register(
        SomeInterface, 'feat.test.test_agents_base_dependency.production',
        ExecMode.production)


@descriptor.register('blah_blah')
class Descriptor(descriptor.Descriptor):
    pass
