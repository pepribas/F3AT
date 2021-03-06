# F3AT - Flumotion Asynchronous Autonomous Agent Toolkit
# Copyright (C) 2010,2011 Flumotion Services, S.A.
# All rights reserved.

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

# See "LICENSE.GPL" in the source distribution for more information.

# Headers in this file shall remain intact.
import sys
import re
import types
import os

from twisted.internet import reactor
from twisted.spread import pb

from feat.agents.base.agent import registry_lookup
from feat.agents.base import recipient, descriptor
from feat.agents.common import host
from feat.agencies import agency, journaler
from feat.agencies.net import ssh, broker
from feat.common import log, defer, time, first, error, run
from feat.common import manhole, text_helper
from feat.process import standalone
from feat.process.base import ProcessState
from feat.gateway import gateway
from feat.utils import locate

from feat.agencies.net import messaging, database, options

from feat.interface.agent import IAgentFactory, AgencyAgentState
from feat.interface.agency import ExecMode
from feat.agencies.interface import NotFoundError
from feat.agencies.net.broker import BrokerRole


GATEWAY_PORT_COUNT = 100
HOST_RESTART_RETRY_INTERVAL = 5


class AgencyAgent(agency.AgencyAgent):

    @manhole.expose()
    def get_gateway_port(self):
        return self.agency.gateway_port


class Agency(agency.Agency):

    agency_agent_factory = AgencyAgent

    broker_factory = broker.Broker

    @classmethod
    def from_config(cls, env, options=None):
        agency = cls()
        agency._load_config(env, options)
        return agency

    def __init__(self,
                 msg_host=options.DEFAULT_MSG_HOST,
                 msg_port=options.DEFAULT_MSG_PORT,
                 msg_user=options.DEFAULT_MSG_USER,
                 msg_password=options.DEFAULT_MSG_PASSWORD,
                 db_host=options.DEFAULT_DB_HOST,
                 db_port=options.DEFAULT_DB_PORT,
                 db_name=options.DEFAULT_DB_NAME,
                 public_key=options.DEFAULT_MH_PUBKEY,
                 private_key=options.DEFAULT_MH_PRIVKEY,
                 authorized_keys=options.DEFAULT_MH_AUTH,
                 manhole_port=options.DEFAULT_MH_PORT,
                 agency_journal=options.DEFAULT_JOURFILE,
                 socket_path=options.DEFAULT_SOCKET_PATH,
                 gateway_port=options.DEFAULT_GW_PORT,
                 enable_spawning_slave=options.DEFAULT_ENABLE_SPAWNING_SLAVE,
                 rundir=None,
                 logdir=None,
                 daemonize=options.DEFAULT_DAEMONIZE,
                 force_host_restart=options.DEFAULT_FORCE_HOST_RESTART):

        agency.Agency.__init__(self)

        curdir = os.path.abspath(os.curdir)
        if rundir is None:
            rundir = options.DEFAULT_RUNDIR if daemonize else curdir
        if logdir is None:
            logdir = options.DEFAULT_LOGDIR if daemonize else curdir

        self._init_config(msg_host=msg_host,
                          msg_port=msg_port,
                          msg_password=msg_password,
                          msg_user=msg_user,
                          db_host=db_host,
                          db_port=db_port,
                          db_name=db_name,
                          public_key=public_key,
                          private_key=private_key,
                          authorized_keys=authorized_keys,
                          manhole_port=manhole_port,
                          agency_journal=agency_journal,
                          socket_path=socket_path,
                          gateway_port=gateway_port,
                          enable_spawning_slave=enable_spawning_slave,
                          rundir=rundir,
                          logdir=logdir,
                          daemonize=daemonize,
                          force_host_restart=force_host_restart)

        self._ssh = None
        self._broker = None
        self._gateway = None

        # this is default mode for the dependency modules
        self._set_default_mode(ExecMode.production)

        # hostdef to pass to the Host Agent we run
        self._hostdef = None

        # flag saying that we are in the process of starting the Host Agent,
        # it's used not to do this more than once
        self._starting_host = False
        # flag set when we enter the agency shutdown. It's used not to trigger
        # starting new host agent while we are shutting down the agency
        self._shutting_down = False
        # list of agent types or descriptors to spawn when the host agent
        # is ready. Format (agent_type_or_desc, args, kwargs)
        self._to_spawn = list()
        # semaphore preventing multiple entries into logic spawning agents
        # by host agent
        self._flushing_sem = defer.DeferredSemaphore(1)

    def wait_event(self, agent_id, event):
        return self._broker.wait_event(agent_id, event)

    @manhole.expose()
    def spawn_agent(self, desc, **kwargs):
        '''tells the host agent running in this agency to spawn a new agent
        of the given type.'''
        self._to_spawn.append((desc, kwargs, ))
        return self._flush_agents_to_spawn()

    def _flush_agents_to_spawn(self):
        return self._flushing_sem.run(self._flush_agents_body)

    @defer.inlineCallbacks
    def _flush_agents_body(self):
        medium = self._get_host_medium()
        if medium is None:
            msg = "Host Agent not ready yet, agent will be spawned later."
            defer.returnValue(msg)
        yield medium.wait_for_state(AgencyAgentState.ready)
        agent = medium.get_agent()
        while True:
            try:
                to_spawn = self._to_spawn.pop(0)
            except IndexError:
                break
            desc, kwargs = to_spawn
            if not isinstance(desc, descriptor.Descriptor):
                factory = descriptor.lookup(desc)
                if factory is None:
                    raise ValueError(
                        'No descriptor factory found for agent %r' % desc)
                desc = factory()
            desc = yield medium.save_document(desc)
            yield agent.start_agent(desc, **kwargs)

    def initiate(self):
        if self.config['agency']['daemonize']:
            os.chdir(self.config['agency']['rundir'])

        mesg = messaging.Messaging(
            self.config['msg']['host'], int(self.config['msg']['port']),
            self.config['msg']['user'], self.config['msg']['password'])
        mesg.redirect_log(self)
        db = database.Database(
            self.config['db']['host'], int(self.config['db']['port']),
            self.config['db']['name'])
        db.redirect_log(self)
        jour = journaler.Journaler(self)
        self._journal_writer = None

        reactor.addSystemEventTrigger('before', 'shutdown',
                                      self.on_killed)

        mc = self.config['manhole']
        ssh_port = int(mc["port"]) if mc["port"] is not None else None
        self._ssh = ssh.ListeningPort(self, ssh.commands_factory(self),
                                      public_key=mc["public_key"],
                                      private_key=mc["private_key"],
                                      authorized_keys=mc["authorized_keys"],
                                      port=ssh_port)

        socket_path = self.config['agency']['socket_path']
        self._broker = self.broker_factory(self, socket_path,
                                on_master_cb=self.on_become_master,
                                on_slave_cb=self.on_become_slave,
                                on_disconnected_cb=self.on_broker_disconnect,
                                on_remove_slave_cb=self.on_remove_slave)

        self._setup_snapshoter()

        d = defer.succeed(None)
        d.addBoth(defer.drop_param, agency.Agency.initiate,
                  self, db, jour, mesg)
        d.addBoth(defer.drop_param, self._broker.initiate_broker)
        d.addBoth(defer.override_result, self)
        return d

    ### public ###

    def set_host_def(self, hostdef):
        '''
        Sets the hostdef param which will get passed to the Host Agent which
        the agency starts if it becomes the master.
        '''
        if not isinstance(hostdef,
                          (host.HostDef, unicode, str, types.NoneType)):
            raise AttributeError("Expected attribute 1 to be a HostDef or "
                                 "a document id got %r instead." %
                                 (hostdef, ))
        if self._hostdef is not None:
            self.info("Overwriting previous hostdef, which was %r",
                      self._hostdef)
        self._hostdef = hostdef

    @property
    def role(self):
        return self._broker.state

    def locate_master(self):
        return (self.get_hostname(), self.config["gateway"]["port"],
                self._broker.state != BrokerRole.master)

    def locate_agency(self, agency_id):

        def pack_result(port, remote):
            return self.get_hostname(), port, remote

        if agency_id == self.agency_id:
            return defer.succeed(pack_result(self.gateway_port, False))

        for slave_id, slave in self._broker.slaves.iteritems():
            if slave_id == agency_id:
                d = slave.callRemote('get_gateway_port')
                d.addCallback(pack_result, True)
                return d

        return defer.succeed(None)

    def on_become_master(self):
        self._ssh.start_listening()
        filename = os.path.join(self.config['agency']['rundir'],
                                self.config['agency']['journal'])
        self._journal_writer = journaler.SqliteWriter(
            self, filename=filename, encoding='zip',
            on_rotate=self._force_snapshot_agents)
        self._journaler.configure_with(self._journal_writer)
        self._journal_writer.initiate()
        self._start_master_gateway()

        self._redirect_text_log()
        self._create_pid_file()
        self._link_log_file(options.MASTER_LOG_LINK)

        if 'enable_host_restart' not in self._broker.shared_state:
            value = self.config['agency']['force_host_restart']
            self._broker.shared_state['enable_host_restart'] = value

        d = defer.succeed(None)
        if self.config['agency']['enable_spawning_slave']:
            d.addCallback(defer.drop_param, self._spawn_backup_agency)

        d.addCallback(defer.drop_param, self._start_host_agent_if_necessary)
        return d

    def on_remove_slave(self):
        return self._spawn_backup_agency()

    def on_become_slave(self):
        self._ssh.stop_listening()
        self._journal_writer = journaler.BrokerProxyWriter(self._broker)
        self._journaler.configure_with(self._journal_writer)
        self._journal_writer.initiate()
        self._redirect_text_log()
        self._start_slave_gateway()

    def _redirect_text_log(self):
        if self.config['agency']['daemonize']:
            log_id = str(self.agency_id)

            logname = "%s.%s.log" % ('feat', log_id, )
            logfile = os.path.join(self.config['agency']['logdir'], logname)
            log.FluLogKeeper.move_files(logfile, logfile)

    def _link_log_file(self, filename):
        if not self.config['agency']['daemonize']:
            return

        logfile, _ = log.FluLogKeeper.get_filenames()
        basedir = os.path.dirname(logfile)
        linkname = os.path.join(basedir, filename)
        try:
            os.unlink(linkname)
        except OSError:
            pass
        try:
            os.link(logfile, linkname)
        except OSError as e:
            self.warning("Failed to link log file %s to %s: %s",
                         logfile, linkname, error.get_exception_message(e))

    def on_broker_disconnect(self, pre_state):
        self._ssh.stop_listening()
        d = self._journaler.close(flush_writer=False)
        d.addCallback(defer.drop_param, self._gateway.cleanup)

        if pre_state == BrokerRole.master:
            d.addCallback(defer.drop_param, run.delete_pidfile,
                          self.config['agency']['rundir'])
        return d

    def get_journal_writer(self):
        '''Called by the broker internals to establish the bridge between
        JournalWriters'''
        return self._journal_writer

    def on_killed(self):
        d = agency.Agency.on_killed(self)
        d.addCallback(defer.drop_param, self._disconnect)
        return d

    @manhole.expose()
    def full_shutdown(self, stop_process=False):
        '''Terminate all the slave agencies and shutdowns itself.'''
        self._shutting_down = True
        d = defer.succeed(None)
        d.addCallback(defer.drop_param, self._broker.shutdown_slaves)
        d.addCallback(defer.drop_param, self.shutdown, stop_process)
        return d

    @manhole.expose()
    def shutdown(self, stop_process=False):
        '''Shutdown the agency in gentel manner (terminate all the agents).'''
        self._shutting_down = True
        self._cancel_snapshoter()
        d = agency.Agency.shutdown(self)
        d.addCallback(defer.drop_param, self._disconnect)
        if stop_process:
            d.addBoth(defer.drop_param, self._stop_process)
        return d

    def upgrade(self, upgrade_cmd, testing=False):
        args = upgrade_cmd.split(" ")
        command = args.pop(0)
        process = standalone.Process(self, command, args, os.environ.copy())

        d = self.full_shutdown()
        d.addBoth(defer.drop_param, process.restart)
        d.addBoth(defer.drop_param, process.wait_for_state,
                  ProcessState.finished, ProcessState.failed)
        if not testing:
            d.addBoth(defer.drop_param, self._stop_process)
        return d

    def _disconnect(self):
        d = defer.succeed(None)
        d.addCallback(defer.drop_param, self._ssh.stop_listening)
        d.addCallback(defer.drop_param, self._gateway.cleanup)
        d.addCallback(defer.drop_param, self._journaler.close)
        d.addCallback(defer.drop_param, self._broker.disconnect)
        return d

    def register_agent(self, medium):
        agency.Agency.register_agent(self, medium)
        self._broker.register_agent(medium)

    def unregister_agent(self, medium):
        agency.Agency.unregister_agent(self, medium)
        agent_id = medium.get_agent_id()
        self._broker.push_event(agent_id, 'unregistered')
        self._broker.unregister_agent(medium)
        self._start_host_agent_if_necessary()

    @manhole.expose()
    def start_agent(self, descriptor, **kwargs):
        """
        Starting an agent is delegated to the broker, who makes sure that
        this method will be eventually run on the master agency.
        """
        return self._broker.start_agent(descriptor, **kwargs)

    def actually_start_agent(self, descriptor, **kwargs):
        """
        This method will be run only on the master agency.
        """
        factory = IAgentFactory(
            registry_lookup(descriptor.document_type))
        if factory.standalone:
            return self.start_standalone_agent(descriptor, factory, **kwargs)
        else:
            return self.start_agent_locally(descriptor, **kwargs)

    def start_agent_locally(self, descriptor, **kwargs):
        return agency.Agency.start_agent(self, descriptor, **kwargs)

    def start_standalone_agent(self, descriptor, factory, **kwargs):
        cmd, cmd_args, env = factory.get_cmd_line(descriptor, **kwargs)
        self._store_config(env)
        recp = recipient.Agent(descriptor.doc_id, descriptor.shard)

        d = self._broker.wait_event(recp.key, 'started')
        d.addCallback(lambda _: recp)

        p = standalone.Process(self, cmd, cmd_args, env)
        p.restart()

        return d

    @manhole.expose()
    @defer.inlineCallbacks
    def locate_agent(self, recp):
        '''locate_agent(recp): Return (host, port, should_redirect) tuple.
        '''
        if recipient.IRecipient.providedBy(recp):
            agent_id = recp.key
        else:
            agent_id = recp
        found = yield self.find_agent(agent_id)
        if isinstance(found, agency.AgencyAgent):
            host = self.get_hostname()
            port = self.gateway_port
            defer.returnValue((host, port, False, ))
        elif isinstance(found, pb.RemoteReference):
            host = self.get_hostname()
            port = yield found.callRemote('get_gateway_port')
            defer.returnValue((host, port, True, ))
        else: # None
            db = self._database.get_connection()
            host = yield locate.locate(db, agent_id)
            port = self.config["gateway"]["port"]
            if host is None:
                defer.returnValue(None)
            else:
                defer.returnValue((host, port, True, ))

    @manhole.expose()
    def reconfigure_messaging(self, msg_host, msg_port):
        '''force messaging reconnector to the connect to the (host, port)'''
        messaging = self._backends["default"]
        messaging.reconfigure(msg_host, msg_port)

    @manhole.expose()
    def reconfigure_database(self, host, port, name='feat'):
        '''force database reconnector to connect to the (host, port, name)'''
        self._database.reconfigure(host, port, name)

    @manhole.expose()
    def show_connections(self):
        t = text_helper.Table(
            fields=("Connection", "Connected", "Host", "Port", "Reconnect in"),
            lengths=(20, 15, 30, 10, 15))
        connections = self._backends.values() + [self._database]
        iterator = (x.show_status() for x in connections)
        return t.render(iterator)

    ### Manhole inspection methods ###

    @manhole.expose()
    def get_gateway_port(self):
        return self._gateway and self._gateway.port

    gateway_port = property(get_gateway_port)

    @manhole.expose()
    def find_agent_locally(self, agent_id):
        '''Same as find_agent but only checks in scope of this agency.'''
        return agency.Agency.find_agent(self, agent_id)

    @manhole.expose()
    def find_agent(self, agent_id):
        '''Gives medium class or its pb refrence of the agent if this agency
        hosts it.'''
        return self._broker.find_agent(agent_id)

    def iter_agency_ids(self):
        return self._broker.iter_agency_ids()

    @manhole.expose()
    @defer.inlineCallbacks
    def list_slaves(self):
        '''Print information about the slave agencies.'''
        resp = []
        for slave_id, slave in self._broker.slaves.iteritems():
            resp += ["#### Slave %s ####" % slave_id]
            table = yield slave.callRemote('list_agents')
            resp += [table]
            resp += []
        defer.returnValue("\n".join(resp))

    @manhole.expose()
    def get_slave(self, slave_id):
        '''Give the reference to the nth slave agency.'''
        return self._broker.slaves[slave_id].reference

    # Config manipulation (standalone agencies receive the configuration
    # in the environment).

    def _init_config(self, msg_host=None, msg_port=None,
                     msg_user=None, msg_password=None,
                     db_host=None, db_port=None, db_name=None,
                     public_key=None, private_key=None,
                     authorized_keys=None, manhole_port=None,
                     agency_journal=None, socket_path=None,
                     gateway_port=None, enable_spawning_slave=None,
                     rundir=None, logdir=None, daemonize=None,
                     force_host_restart=None):

        msg_conf = dict(host=msg_host,
                        port=msg_port,
                        user=msg_user,
                        password=msg_password)

        db_conf = dict(host=db_host,
                       port=db_port,
                       name=db_name)

        manhole_conf = dict(public_key=public_key,
                            private_key=private_key,
                            authorized_keys=authorized_keys,
                            port=manhole_port)

        if socket_path and not os.path.isabs(socket_path):
            socket_path = os.path.join(rundir, socket_path)

        agency_conf = dict(journal=agency_journal,
                           socket_path=socket_path,
                           rundir=rundir,
                           logdir=logdir,
                           enable_spawning_slave=enable_spawning_slave,
                           daemonize=daemonize,
                           force_host_restart=force_host_restart)

        gateway_conf = dict(port=gateway_port)

        self.config = dict()
        self.config['msg'] = msg_conf
        self.config['db'] = db_conf
        self.config['manhole'] = manhole_conf
        self.config['agency'] = agency_conf
        self.config['gateway'] = gateway_conf

    def _store_config(self, env):
        '''
        Stores agency config into environment to be read by the
        standalone agency.'''
        for key in self.config:
            for kkey in self.config[key]:
                var_name = "FEAT_%s_%s" % (key.upper(), kkey.upper(), )
                env[var_name] = str(self.config[key][kkey])

    def _load_config(self, env, options=None):
        '''
        Loads config from environment.
        Environment values can be overridden by specified options.
        '''
        # First load from env
        matcher = re.compile('\AFEAT_([^_]+)_(.+)\Z')
        for key in env:
            res = matcher.search(key)
            if res:
                c_key = res.group(1).lower()
                c_kkey = res.group(2).lower()
                value = str(env[key])
                if value == 'None':
                    value = None
                if c_key in self.config:
                    self.log("Setting %s.%s to %r", c_key, c_kkey, value)
                    self.config[c_key][c_kkey] = value

        #Then override with options
        if options:
            for group_key, conf_group in self.config.items():
                for conf_key in conf_group:
                    attr = "%s_%s" % (group_key, conf_key)
                    if hasattr(options, attr):
                        new_value = getattr(options, attr)
                        old_value = conf_group[conf_key]
                        if new_value is not None and (old_value != new_value):
                            if old_value is None:
                                self.log("Setting %s.%s to %r",
                                         group_key, conf_key, new_value)
                            else:
                                self.log("Overriding %s.%s to %r",
                                         group_key, conf_key, new_value)
                            conf_group[conf_key] = new_value

    def _start_host_agent_if_necessary(self):
        '''
        This method starts saves the host agent descriptor and runs it.
        To make this happen following conditions needs to be fulfilled:
        - it is a master agency,
        - we are not starting a host agent already,
        - we are not terminating,
        - and last but not least, we dont have a host agent running.
        '''

        def set_flag(value):
            self._starting_host = value


        if self.role != BrokerRole.master:
            self.log('Not starting host agent, because we are not the '
                     'master agency')
            return

        if self._shutting_down:
            self.log('Not starting host agent, because the agency '
                     'is about to terminate itself')
            return

        if self._get_host_agent():
            self.log('Not starting host agent, because we already '
                     ' have one')
            return

        if self._starting_host:
            self.log('Not starting host agent, because we are already '
                     'starting one.')
            return

        def handle_error_on_get(fail, connection, doc_id):
            fail.trap(NotFoundError)
            desc = host.Descriptor(shard=u'lobby', doc_id=doc_id)
            self.log("Host Agent descriptor not found in database, "
                     "creating a brand new instance.")
            return connection.save_document(desc)

        def handle_success_on_get(desc):
            if not self._broker.shared_state['enable_host_restart']:
                msg = ("Descriptor of host agent has been found in "
                       "database (hostname: %s). This should not happen "
                       "on the first run. If you really want to restart "
                       "the host agent include --force-host-restart "
                       "options to feat script." % desc.doc_id)
                self.error(msg)
                d = self.full_shutdown(stop_process=True)
                d.addBoth(defer.raise_error, RuntimeError, msg)
                return d
            self.log("Host Agent descriptor found in database, will restart.")
            return desc

        set_flag(True)
        self.info('Starting host agent.')
        conn = self._database.get_connection()

        doc_id = self.get_hostname()
        d = defer.Deferred()
        d.addCallback(defer.drop_param, self.wait_connected)
        d.addCallback(defer.drop_param, conn.get_document, doc_id)
        d.addCallbacks(handle_success_on_get, handle_error_on_get,
                       errbackArgs=(conn, doc_id, ))
        d.addCallback(self.start_agent, hostdef=self._hostdef)
        d.addBoth(defer.bridge_param, set_flag, False)
        d.addCallback(defer.drop_param, self._broker.shared_state.__setitem__,
                      'enable_host_restart', True)
        d.addCallback(defer.drop_param, self._flush_agents_to_spawn)
        d.addErrback(self._host_restart_failed)

        time.callLater(0, d.callback, None)

    def _host_restart_failed(self, failure):
        error.handle_failure(self, failure, "Failure during host restart")
        self.debug("Retrying in %d seconds",
                   HOST_RESTART_RETRY_INTERVAL)
        time.callLater(HOST_RESTART_RETRY_INTERVAL,
                       self._start_host_agent_if_necessary)

    def _get_host_agent(self):
        medium = self._get_host_medium()
        return medium and medium.get_agent()

    def _get_host_medium(self):
        return first((x for x in self._agents
                      if x.get_descriptor().document_type == 'host_agent'))

    @defer.inlineCallbacks
    def _find_agent(self, agent_id):
        '''
        Specific to master agency, called by the broker.
        Will return AgencyAgent if agent is hosted by master agency,
        PB.Reference if it runs in stanadlone or None if it was not found.
        '''
        local = yield self.find_agent_locally(agent_id)
        if local:
            defer.returnValue(local)
        for slave in self._broker.iter_slaves():
            found = yield slave.callRemote('find_agent_locally', agent_id)
            if found:
                defer.returnValue(found)
        defer.returnValue(None)

    @manhole.expose()
    def snapshot_agents(self, force=False):
        agency.Agency.snapshot_agents(self, force)
        if force:
            return self._broker.broadcast_force_snapshot()

    def _setup_snapshoter(self):
        self._snapshot_task = time.callLater(300, self._trigger_snapshot)

    def _trigger_snapshot(self):
        self.log("Snapshoting all the agents.")
        self.snapshot_agents()
        self._snapshot_task = None
        self._setup_snapshoter()

    def _force_snapshot_agents(self):
        self.log("Journal has been rotated, forcing snapshot of agents")
        # TODO: Mind also the agents running in slave agencies
        self.snapshot_agents(force=True)

    def _cancel_snapshoter(self):
        if self._snapshot_task is not None and self._snapshot_task.active():
            self._snapshot_task.cancel()
        self._snapshot_task = None

    def _start_slave_gateway(self):
        master_port = int(self.config["gateway"]["port"])
        range = (master_port, master_port + GATEWAY_PORT_COUNT)
        self._gateway = gateway.Gateway(self, range)
        self._gateway.initiate_slave()

    def _start_master_gateway(self):
        master_port = int(self.config["gateway"]["port"])
        range = (master_port, master_port + GATEWAY_PORT_COUNT)
        self._gateway = gateway.Gateway(self, range)
        self._gateway.initiate_master()

    def _create_pid_file(self):
        rundir = self.config['agency']['rundir']
        pid_file = run.acquire_pidfile(rundir)

        path = run.write_pidfile(rundir, file=pid_file)
        self.log("Written pid file %s" % path)

    def _spawn_backup_agency(self):

        def get_cmd_line():
            python_path = ":".join(sys.path)
            path = os.environ.get("PATH", "")
            feat_debug = self.get_logging_filter()

            command = 'feat'
            args = ['-D']
            env = dict(PYTHONPATH=python_path,
                       FEAT_DEBUG=feat_debug,
                       PATH=path)
            return command, args, env

        if self._shutting_down:
            return

        if self._broker.is_master() and not self._broker.has_slave():
            self.log("Spawning backup agency")
            cmd, cmd_args, env = get_cmd_line()
            self._store_config(env)

            p = standalone.Process(self, cmd, cmd_args, env)
            return p.restart()
        else:
            return

    ### slave agency process related

    def _stop_process(self):
        reactor.stop()
