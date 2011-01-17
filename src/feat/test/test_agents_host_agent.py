from feat.agents.base import testsuite, recipient, message
from feat.agents.host import host_agent
from feat.common import fiber
from feat.test import factories


class TestHostAgent(testsuite.TestCase):

    def setUp(self):
        testsuite.TestCase.setUp(self)
        instance = self.ball.generate_agent(host_agent.HostAgent)
        self.agent = self.ball.load(instance)

    def testInitiate(self):
        recp = recipient.Agent('join-shard', 'lobby')
        manager = self.ball.generate_manager(
            self.agent, host_agent.JoinShardManager)
        expected = [
            testsuite.side_effect('AgencyAgent.register_interest',
                                  args=(host_agent.StartAgentReplier, )),
            testsuite.side_effect('AgencyAgent.retrying_protocol',
                                  args=(host_agent.JoinShardManager, recp),
                                  result=manager)]
        f, state = self.ball.call(expected, self.agent.initiate)
        self.assertFiberTriggered(f, fiber.TriggerType.succeed)
        self.assertFiberCalls(f, manager.notify_finish)

    def testSwithShard(self):
        old_shard = self.ball.descriptor.shard
        dest_shard = 'some shard'
        desired_descriptor = testsuite.CompareObject(host_agent.Descriptor,
                                                     shard=dest_shard)
        expected = [
            testsuite.side_effect('AgencyAgent.get_descriptor',
                                  result=self.ball.descriptor),
            testsuite.side_effect('AgencyAgent.leave_shard',
                                  args=(old_shard, )),
            testsuite.side_effect('AgencyAgent.join_shard',
                                  args=(dest_shard, ))]
        f, s = self.ball.call(expected, self.agent.switch_shard, dest_shard)
        self.assertFiberTriggered(f, fiber.TriggerType.succeed,
                                  desired_descriptor)
        self.assertFiberCalls(f, s.medium.update_descriptor)

    def testStartAgent(self):
        desc = factories.build('descriptor')
        f, state = self.ball.call(None, self.agent.start_agent, desc)
        self.assertFiberTriggered(f, fiber.TriggerType.succeed, desc)
        self.assertFiberCalls(f, state.medium.start_agent)


class TestJoinShardManager(testsuite.TestCase):

    def setUp(self):
        testsuite.TestCase.setUp(self)
        agent = self.ball.generate_agent(host_agent.HostAgent)
        instance = self.ball.generate_manager(agent,
                                              host_agent.JoinShardManager)
        self.manager = self.ball.load(instance)

    def testInitiate(self):
        address = recipient.Agent(agent_id=self.ball.descriptor.doc_id,
                                  shard=self.ball.descriptor.shard)
        args = (
            testsuite.message(payload=dict(level=0, joining_agent=address)), )
        expected = [
            testsuite.side_effect('AgencyAgent.get_descriptor',
                                  self.ball.descriptor),
            testsuite.side_effect('AgencyManager.announce', args=args)]
        output, state = self.ball.call(expected, self.manager.initiate)

    def testClosed(self):
        bids = [
            message.Bid(payload=dict(idd="best", cost=1)),
            message.Bid(payload=dict(idd="worse", cost=10))]
        sfx = [
            testsuite.side_effect('AgencyManager.get_bids', bids),
            testsuite.side_effect('AgencyAgent.get_descriptor',
                                  self.ball.descriptor),
            testsuite.side_effect('AgencyManager.grant',
                                  args=((bids[0], testsuite.whatever, ), ))]
        self.ball.call(sfx, self.manager.closed)

    def testCompleted(self):
        report = message.FinalReport(payload=dict(shard='new shard'))
        f, s = self.ball.call(None, self.manager.completed, (report, ))
        self.assertFiberTriggered(f, fiber.TriggerType.succeed, 'new shard')
        self.assertFiberCalls(f, s.agent.switch_shard)