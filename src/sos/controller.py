#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.
import tempfile
import os
import sys
import zmq
import time
import threading
import uuid
from collections import defaultdict
from .utils import env, ProcessKilled, get_localhost_ip, get_open_files_and_connections
from .signatures import StepSignatures, WorkflowSignatures
from .messages import encode_msg, decode_msg

EVENT_MAP = {}
for name in ('PUSH', 'PULL', 'PAIR', 'REQ', 'REP'):
    EVENT_MAP[getattr(zmq, name)] = name

g_sockets = set()


def create_socket(context, socket_type, desc=''):
    socket = context.socket(socket_type)
    g_sockets.add(socket.fd)
    if 'CONTROLLER' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
            'SOS_DEBUG']:
        env.log_to_file(
            'CONTROLLER',
            f'{os.getpid()} {desc}: new socket of type {EVENT_MAP.get(socket_type, "UNKNOWN")} with handler {socket.fd} ({len(g_sockets)} total)'
        )
    return socket


def close_socket(socket, desc='', now=False):
    if socket is None:
        return
    g_sockets.remove(socket.fd)
    if 'CONTROLLER' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
            'SOS_DEBUG']:
        env.log_to_file(
            'CONTROLLER',
            f'{os.getpid()} {desc}: closes socket with handler {socket.fd} ({len(g_sockets)} left)'
        )
    if now:
        socket.LINGER = 0
    socket.close()
    return socket


def zmq_term(context):
    #
    # the following is only valid when multiprocessing is started with the spawn method
    # otherwise g_sockets will contain sockets from the parental process

    # if g_sockets:
    # env.logger.warning(f'{os.getpid()} terminting zmq with {len(g_sockets)} unclosed sockets: {g_sockets}')
    context.term()


def send_message_to_controller(msg):
    if env.master_push_socket is None:
        env.master_push_socket = create_socket(env.zmq_context, zmq.PUSH,
                                               'master push')
        env.master_push_socket.connect(env.config["sockets"]["master_push"])
    env.master_push_socket.send(encode_msg(msg))


def request_answer_from_controller(msg):
    if env.master_request_socket is None:
        env.master_request_socket = create_socket(env.zmq_context, zmq.REQ,
                                                  'master request')
        env.master_request_socket.connect(
            env.config["sockets"]["master_request"])
    env.master_request_socket.send(encode_msg(msg))
    return decode_msg(env.master_request_socket.recv())


def connect_controllers(context=None):
    if not context:
        if 'CONTROLLER' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                'SOS_DEBUG']:
            env.log_to_file('CONTROLLER', f'create context at {os.getpid()}')
        context = zmq.Context()

    if 'CONTROLLER' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
            'SOS_DEBUG']:
        env.log_to_file('CONTROLLER', f'Connecting sockets from {os.getpid()}')

    env.master_push_socket = None
    env.master_request_socket = None

    # if this instance of sos is being tapped. It should connect to a few sockets
    #
    if env.config['exec_mode'] == 'slave':
        env.tapping_logging_socket = create_socket(context, zmq.PUSH)
        env.tapping_logging_socket.connect(
            env.config["sockets"]["tapping_logging"])
        # change logging to socket
        env.set_socket_logger(env.tapping_logging_socket)

    # master also need to update task status from interactive runner.
    if env.config['exec_mode'] in ('master', 'slave'):
        env.tapping_listener_socket = create_socket(context, zmq.PUSH)
        env.tapping_listener_socket.connect(
            env.config["sockets"]["tapping_listener"])

    return context


def disconnect_controllers(context=None):
    close_socket(env.master_push_socket, now=True)
    close_socket(env.master_request_socket, now=True)

    if env.config['exec_mode'] == 'slave':
        close_socket(env.tapping_logging_socket, now=True)
        env.set_socket_logger(None)

    if env.config['exec_mode'] in ('master', 'slave'):
        close_socket(env.tapping_listener_socket, now=True)

    if 'CONTROLLER' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
            'SOS_DEBUG']:
        env.log_to_file('CONTROLLER',
                        f'Disconnecting sockets from {os.getpid()}')

    if context:
        if 'CONTROLLER' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                'SOS_DEBUG']:
            env.log_to_file('CONTROLLER', f'terminate context at {os.getpid()}')
        zmq_term(context)


class DotProgressBar:

    def __init__(self, context, interval=1):
        self.context = context
        self.interval = interval * 1000

        self._subprogressbar_size = 25
        self._substep_last_updated = time.time()

        self.stop_event = threading.Event()
        self._substep_cnt = 0

        # broker to handle the execution of substeps
        self.progress_push_socket = create_socket(self.context, zmq.PUSH,
                                                  'progress push')
        self.progress_port = self.progress_push_socket.bind_to_random_port(
            'tcp://127.0.0.1')

        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def run(self):
        progress_pull_socket = create_socket(self.context, zmq.PULL,
                                             'progress pull')
        progress_pull_socket.connect(f'tcp://127.0.0.1:{self.progress_port}')

        # leading progress bar
        sys.stderr.write('\033[32m[\033[0m')
        sys.stderr.flush()

        _pulse_cnt = 0
        while True:
            # no new message, add pulse
            if self.stop_event.is_set():
                return
            if not progress_pull_socket.poll(self.interval):
                if _pulse_cnt == 10:
                    sys.stderr.write('\b \b' * _pulse_cnt)
                    _pulse_cnt = 0
                else:
                    sys.stderr.write('\033[97m.\033[0m')
                    _pulse_cnt += 1
                sys.stderr.flush()
            else:
                msg = progress_pull_socket.recv().decode()
                # print update message
                sys.stderr.write('\b \b' * _pulse_cnt + msg)
                _pulse_cnt = 0
                sys.stderr.flush()

    def update(self, prog_type, status=None):
        if prog_type == 'substep_ignored':
            if time.time() - self._substep_last_updated < 1:
                return
            if self._substep_cnt == self._subprogressbar_size:
                update_str = '\b \b' * self._substep_cnt + '\033[90m.\033[0m'
                self._substep_cnt = 0
            else:
                update_str = '\033[90m.\033[0m'
            self._substep_cnt += 1
            self._substep_last_updated = time.time()
        elif prog_type == 'substep_completed':
            if time.time() - self._substep_last_updated < 1:
                return
            if self._substep_cnt == self._subprogressbar_size:
                update_str = '\b \b' * self._substep_cnt + '\033[32m.\033[0m'
                self._substep_cnt = 0
            else:
                update_str = '\033[32m.\033[0m'
            self._substep_cnt += 1
            self._substep_last_updated = time.time()
        elif prog_type == 'step_completed':
            update_str = '\b \b' * self._substep_cnt
            self._substep_cnt = 0
            if status == 1:  # completed
                update_str += '\033[32m#\033[0m'
            elif status == 0:  # completed
                update_str += '\033[90m#\033[0m'
            elif status > 0:  # in the middle
                update_str += '\033[36m#\033[0m'
            else:  # untracked (no signature)
                update_str += '\033[33m#\033[0m'
        elif prog_type == 'done':
            update_str = '\b \b' * self._substep_cnt + f'\033[32m]\033[0m {status}\n'
            self._substep_cnt = 0

        self.progress_push_socket.send(update_str.encode())

    def done(self, msg):
        self.update('done', msg)
        self.stop_event.set()


class Controller(threading.Thread):
    '''This controller is used by both sos and sos-notebook, and there
    can be two controllers one as a slave (sos) and one as a master
    (notebook). We shared the same code base because step executors need
    need to talk to the same controller (signature, controller etc) when
    they are executed in sos or sos notebook.
    '''

    def __init__(self, ready, kernel=None):
        threading.Thread.__init__(self)
        #self.daemon = True

        self.step_signatures = StepSignatures()
        self.workflow_signatures = WorkflowSignatures()

        self.tapping_controller_socket = None
        self.tapping_listener_socket = None

        self.ready = ready
        self.kernel = kernel
        # number of active running master processes
        self._nprocs = 0

        self._completed = defaultdict(int)
        self._ignored = defaultdict(int)

        # completed steps
        self._completed_steps = {}

        # substep workers
        self.workers = None

        # available resources
        self._resources = {}

        # temporary files
        self._tempfiles = {}

        # self.event_map = {}
        # for name in dir(zmq):
        #     if name.startswith('EVENT_'):
        #         value = getattr(zmq, name)
        #          self.event_map[value] = name
        self.console_logger = None

    def handle_master_push_msg(self, msg):
        try:
            if msg[0] in ('substep', 'step', 'workflow', 'task'):
                # cache the request, route to first available worker
                self.workers.add_request(msg[0], msg[1])
            elif msg[0] == 'nprocs':
                if 'CONTROLLER' in env.config[
                        'SOS_DEBUG'] or 'ALL' in env.config['SOS_DEBUG']:
                    env.log_to_file('CONTROLLER',
                                    f'Active running process set to {msg[1]}')
                self._nprocs = msg[1]
            elif msg[0] == 'progress':
                if msg[1] == 'substep_ignored':
                    self._ignored[msg[2]] += 1
                elif msg[1] == 'substep_completed':
                    self._completed[msg[2]] += 1
                elif msg[1] == 'step_completed':
                    self._completed_steps[msg[3]] = msg[4]
                if env.verbosity == 0:
                    # update progress bar
                    self._progress_bar.update(msg[1],
                                              msg[2] if len(msg) > 2 else None)
            elif msg[0] == 'workflow_sig':
                self.workflow_signatures.write(*msg[1:])
            elif msg[0] == 'step_sig':
                self.step_signatures.set(*msg[1:])
            elif msg[0] == 'commit_sig':
                self.workflow_signatures.commit()
                self.step_signatures.commit()
            elif msg[0] == 'resource':
                if msg[1] == 'docker_image':
                    if msg[2] in ('available', 'unavailable'):
                        if 'docker_image' not in self._resources:
                            self._resources['docker_image'] = {}
                        self._resources['docker_image'][msg[3]] = msg[2]
                    else:
                        raise ValueError(
                            f'SoS currently only understand "available" or "unavailable" messages for docker resource.'
                        )
                elif msg[1] == 'singularity_image':
                    if msg[2] in ('available', 'unavailable'):
                        if 'singularity_image' not in self._resources:
                            self._resources['singularity_image'] = {}
                        self._resources['singularity_image'][msg[3]] = msg[2]
                    else:
                        raise ValueError(
                            f'SoS currently only understand "available" or "unavailable" messages for docker resource.'
                        )
                else:
                    raise ValueError(
                        f'SoS currently does not handle resource of {msg[1]} type'
                    )

            else:
                env.logger.warning(f'Unknown message passed {msg}')
        except Exception as e:
            env.logger.warning(
                f'Failed to handle master push message {msg}: {e}')

    def handle_master_request_msg(self, msg):
        try:
            # make sure all records have been saved before returning information
            while True:
                if self.master_push_socket.poll(0):
                    self.handle_master_push_msg(
                        decode_msg(self.master_push_socket.recv()))
                else:
                    break
            if msg[0] == 'workflow_sig':
                if msg[1] == 'clear':
                    self.workflow_signatures.clear()
                    self.master_request_socket.send(encode_msg('ok'))
                elif msg[1] == 'placeholders':
                    self.master_request_socket.send(
                        encode_msg(
                            self.workflow_signatures.placeholders(msg[2])))
                elif msg[1] == 'records':
                    self.master_request_socket.send(
                        encode_msg(self.workflow_signatures.records(msg[2])))
                else:
                    env.logger.warning(f'Unknown signature request {msg}')
            elif msg[0] == 'step_sig':
                if msg[1] == 'get':
                    self.master_request_socket.send(
                        encode_msg(self.step_signatures.get(*msg[2:])))
                else:
                    env.logger.warning(f'Unknown signature request {msg}')
            elif msg[0] == 'sos_tempfile':
                path, name, suffix, prefix, dir = msg[1:]
                if path is not None:
                    if path not in self._tempfiles.values():
                        if not name:
                            name = uuid.uuid4().hex
                        self._tempfiles[name] = path
                    self.master_request_socket.send(encode_msg(path))
                elif name is not None and name in self._tempfiles:
                    self.master_request_socket.send(
                        encode_msg(self._tempfiles[name]))
                else:
                    #
                    basename = uuid.uuid4().hex
                    filename = basename
                    if name is None:
                        name = basename
                    if suffix is not None:
                        filename = filename + suffix
                    if prefix is not None:
                        filename = prefix + filename
                    if dir is not None:
                        filename = os.path.join(dir, filename)
                    else:
                        filename = os.path.join(tempfile.gettempdir(), filename)
                    #
                    self._tempfiles[name] = filename
                    self.master_request_socket.send(encode_msg(filename))
            elif msg[0] == 'nprocs':
                self.master_request_socket.send(encode_msg(self._nprocs))
            elif msg[0] == 'sos_step':
                self.master_request_socket.send(
                    encode_msg(msg[1] in self._completed_steps or msg[1] in [
                        x.rsplit('_', 1)[0]
                        for x in self._completed_steps.keys()
                    ]))
            elif msg[0] == 'step_output':
                step_name = msg[1]
                if step_name in self._completed_steps:
                    self.master_request_socket.send(
                        encode_msg(self._completed_steps[step_name]))
                else:
                    # now, step_name might actually be a workflow name, in which
                    # case we need to return the last step of the workflow
                    steps = sorted([
                        x for x in self._completed_steps.keys()
                        if x.rsplit('_', 1)[0] == step_name
                    ])
                    self.master_request_socket.send(
                        encode_msg(self._completed_steps[steps[-1]]
                                   if steps else None))
            elif msg[0] == 'named_output':
                name = msg[1]
                found = False
                for step_output in self._completed_steps.values():
                    if name in step_output.labels:
                        found = True
                        self.master_request_socket.send(
                            encode_msg(step_output[name]))
                        break
                if not found:
                    self.master_request_socket.send(encode_msg(None))
            elif msg[0] == 'worker_available':
                self.master_request_socket.send(
                    encode_msg(self.workers.worker_available(msg[1], msg[2:])))
            elif msg[0] == 'resource':
                if msg[1] == 'docker_image':
                    if msg[2] == 'request':
                        if 'docker_image' not in self._resources:
                            self._resources['docker_image'] = {}
                        if msg[3] in self._resources['docker_image']:
                            self.master_request_socket.send(
                                encode_msg(
                                    self._resources['docker_image'][msg[3]]))
                        else:
                            self._resources['docker_image'][msg[3]] = 'pending'
                            self.master_request_socket.send(
                                encode_msg('help yourself'))
                    else:
                        raise ValueError(
                            f'SoS currently only accept request inquiry for docker resource'
                        )
                elif msg[1] == 'singularity_image':
                    if msg[2] == 'request':
                        if 'singularity_image' not in self._resources:
                            self._resources['singularity_image'] = {}
                        if msg[3] in self._resources['singularity_image']:
                            self.master_request_socket.send(
                                encode_msg(self._resources['singularity_image'][
                                    msg[3]]))
                        else:
                            self._resources['singularity_image'][
                                msg[3]] = 'pending'
                            self.master_request_socket.send(
                                encode_msg('help yourself'))
                    else:
                        raise ValueError(
                            f'SoS currently only accept request inquiry for docker resource'
                        )
                else:
                    raise ValueError(
                        f'SoS currently does not handle resource of {msg[1]} type'
                    )
            elif msg[0] == 'done':
                # handle all ctl_push_msgs #1062
                while True:
                    if self.master_push_socket.poll(0):
                        self.handle_master_push_msg(
                            decode_msg(self.master_push_socket.recv()))
                    else:
                        break

                # handle all push request from logging
                if env.config['exec_mode'] in ('master', 'both'):
                    while True:
                        if self.tapping_logging_socket.poll(0):
                            self.handle_tapping_logging_msg(
                                self.tapping_logging_socket.recv_multipart())
                        else:
                            break

                if env.verbosity == 0:
                    num_steps = len(
                        set(self._completed.keys())
                        | set(self._ignored.keys()))
                    num_completed = sum(self._completed.values())
                    num_ignored = sum(self._ignored.values())
                    completed_text = f'{num_completed} job{"s" if num_completed > 1 else ""} completed' if num_completed else ''
                    ignored_text = f'{num_ignored} job{"s" if num_ignored > 1 else ""} ignored' if num_ignored else ''
                    steps_text = f'{num_steps} step{"s" if num_steps > 1 else ""} processed'
                    succ = '' if msg[1] else 'Failed with '
                    self._progress_bar.done(
                        f'{succ}{steps_text} ({completed_text}{", " if num_completed and num_ignored else ""}{ignored_text})'
                    )

                self.master_request_socket.send(encode_msg('bye'))

                return False
            else:
                raise RuntimeError(f'Unrecognized request {msg}')
            return True
        except Exception as e:
            env.logger.warning(f'Failed to respond controller {msg}: {e}')
            self.master_request_socket.send(encode_msg(None))

    def handle_worker_backend_msg(self, msg):
        # msg should be a port number from the worker
        self.workers.process_request(msg[0], msg[1:])

    def handle_tapping_logging_msg(self, msg):
        if env.config['exec_mode'] == 'both':
            print(' '.join(x.decode() for x in msg))
        elif msg[0] == b'ERROR':
            env.logger.error(msg[1].decode())
        elif msg[0] == b'WARNING':
            env.logger.warning(msg[1].decode())
        elif msg[0] == b'INFO':
            env.logger.info(msg[1].decode())
        elif msg[0] == b'DEBUG':
            env.logger.debug(msg[1].decode())
        elif msg[0] == b'TRACE':
            if 'CONTROLLER' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                    'SOS_DEBUG']:
                env.log_to_file('CONTROLLER', msg[1].decode())
        elif msg[0] == b'PRINT':
            env.logger.print(*[x.decode() for x in msg[1:]])
        else:
            print(' '.join(x.decode() for x in msg))

    def handle_tapping_listener_msg(self, msg):
        try:
            #env.log_to_file(f'listener got {msg}')
            self.kernel.send_frontend_msg(msg['msg_type'], msg['data'])
        except Exception as e:
            env.log_to_file(
                f'Failed to handle tapping listerner message {msg}: {e}')

    def handle_tapping_controller_msg(self, msg):
        self.tapping_controller_socket.send(b'ok')

    def run(self):
        # there are two sockets
        #
        self.context = zmq.Context.instance()
        local_ip = get_localhost_ip()

        if 'CONTROLLER' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                'SOS_DEBUG']:
            env.log_to_file('CONTROLLER', f'controller started {os.getpid()}')

        if 'sockets' not in env.config:
            env.config['sockets'] = {}

        self.master_push_socket = create_socket(self.context, zmq.PULL,
                                                'controller master_pull')
        master_push_port = self.master_push_socket.bind_to_random_port(
            f'tcp://{local_ip}')
        env.config['sockets'][
            'master_push'] = f'tcp://{local_ip}:{master_push_port}'

        self.master_request_socket = create_socket(self.context, zmq.REP,
                                                   'controller master_request')
        master_request_port = self.master_request_socket.bind_to_random_port(
            f'tcp://{local_ip}')
        env.config['sockets'][
            'master_request'] = f'tcp://{local_ip}:{master_request_port}'

        # broker to handle the execution of substeps
        self.worker_backend_socket = create_socket(
            self.context, zmq.REP, 'controller backend rep')  # ROUTER
        # we assume the router is always on local host, but we will use a non-localhost
        # IP so that others can connect to it.
        worker_port = self.worker_backend_socket.bind_to_random_port(
            f'tcp://{local_ip}')
        env.config['sockets'][
            'worker_backend'] = f'tcp://{local_ip}:{worker_port}'

        # tapping
        if env.config['exec_mode'] == 'master':
            self.tapping_logging_socket = create_socket(self.context, zmq.PULL)
            tapping_logging_port = self.tapping_logging_socket.bind_to_random_port(
                f'tcp://{local_ip}')
            env.config['sockets'][
                'tapping_logging'] = f'tcp://{local_ip}:{tapping_logging_port}'

            self.tapping_listener_socket = create_socket(self.context, zmq.PULL)
            tapping_listener_port = self.tapping_listener_socket.bind_to_random_port(
                f'tcp://{local_ip}')
            env.config['sockets'][
                'tapping_listener'] = f'tcp://{local_ip}:{tapping_listener_port}'

            self.tapping_controller_socket = create_socket(
                self.context, zmq.PUSH)
            tapping_controller_port = self.tapping_controller_socket.bind_to_random_port(
                f'tcp://{local_ip}')
            env.config['sockets'][
                'tapping_controller'] = f'tcp://{local_ip}:{tapping_controller_port}'

        if env.config['exec_mode'] == 'slave':
            self.tapping_controller_socket = create_socket(
                self.context, zmq.PULL)
            self.tapping_controller_socket.connect(
                env.config["sockets"]["tapping_controller"])

        #monitor_socket = self.master_request_socket.get_monitor_socket()
        # tell others that the sockets are ready
        self.ready.set()

        # create a manager
        from .workers import WorkerManager
        self.workers = WorkerManager(env.config['worker_procs'],
                                     self.worker_backend_socket)

        # Process messages from receiver and controller
        poller = zmq.Poller()
        poller.register(self.master_push_socket, zmq.POLLIN)
        poller.register(self.master_request_socket, zmq.POLLIN)
        poller.register(self.worker_backend_socket, zmq.POLLIN)
        if env.config['exec_mode'] == 'master':
            poller.register(self.tapping_logging_socket, zmq.POLLIN)
            poller.register(self.tapping_listener_socket, zmq.POLLIN)
        if env.config['exec_mode'] == 'slave':
            poller.register(self.tapping_controller_socket, zmq.POLLIN)

        #poller.register(monitor_socket, zmq.POLLIN)
        if env.verbosity == 0:
            # leading progress bar
            self._progress_bar = DotProgressBar(self.context)

        try:
            while True:
                if 'OPENFILES' in env.config[
                        'SOS_DEBUG'] or 'ALL' in env.config['SOS_DEBUG']:
                    ofc = get_open_files_and_connections(os.getpid())
                    env.log_to_file(
                        'OPENFILES',
                        f'CONTROLLER {os.getpid()} has {len(ofc["open_files"])} open files and {len(ofc["connections"])} connections.'
                    )
                while True:
                    socks = dict(poller.poll(1000))
                    if socks:
                        break
                    # if the last worker has been pending for more than 5
                    # seconds, kill it. It is also possible that some others are killed
                    # by external process.
                    self.workers.check_workers()

                if self.master_push_socket in socks:
                    while True:
                        if self.master_push_socket.poll(0):
                            self.handle_master_push_msg(
                                decode_msg(self.master_push_socket.recv()))
                        else:
                            break

                if self.master_request_socket in socks:
                    if not self.handle_master_request_msg(
                            decode_msg(self.master_request_socket.recv())):
                        break

                if self.worker_backend_socket in socks:
                    while True:
                        if self.worker_backend_socket.poll(0):
                            self.handle_worker_backend_msg(
                                decode_msg(self.worker_backend_socket.recv()))
                        else:
                            break

                if env.config['exec_mode'] == 'master':
                    if self.tapping_logging_socket in socks:
                        self.handle_tapping_logging_msg(
                            self.tapping_logging_socket.recv_multipart())
                    if self.tapping_listener_socket in socks:
                        self.handle_tapping_listener_msg(
                            decode_msg(self.tapping_listener_socket.recv()))

                if env.config['exec_mode'] == 'slave':
                    if self.tapping_controller_socket in socks:
                        self.handle_tapping_controller_msg(
                            decode_msg(self.tapping_controller_socket.recv()))

                # if monitor_socket in socks:
                #     evt = recv_monitor_message(monitor_socket)
                #     if evt['event'] == zmq.EVENT_ACCEPTED:
                #         self._num_clients += 1
                #     elif evt['event'] == zmq.EVENT_DISCONNECTED:
                #         self._num_clients -= 1
        except ProcessKilled as e:
            env.logger.error(str(e))
            os._exit(1)
        except Exception as e:
            sys.stderr.write(f'{env.config["exec_mode"]} get an error {e}')
            return
        finally:
            # remove temporary files
            for name, tempfile in self._tempfiles.items():
                try:
                    if os.path.isfile(tempfile):
                        os.remove(tempfile)
                except Exception as e:
                    env.logger.warning(
                        f'Failed to tempfile associated with name {name}')
            # kill all workers
            self.workers.kill_all()

            # close all databses
            self.step_signatures.close()
            self.workflow_signatures.close()

            poller.unregister(self.master_push_socket)
            poller.unregister(self.master_request_socket)
            poller.unregister(self.worker_backend_socket)
            if env.config['exec_mode'] == 'master':
                poller.unregister(self.tapping_logging_socket)
                poller.unregister(self.tapping_listener_socket)
            if env.config['exec_mode'] == 'slave':
                poller.unregister(self.tapping_controller_socket)

            close_socket(self.master_push_socket, now=True)
            close_socket(self.master_request_socket, now=True)
            close_socket(self.worker_backend_socket, now=True)

            if env.config['exec_mode'] == 'master':
                close_socket(self.tapping_logging_socket, now=True)
                close_socket(self.tapping_listener_socket, now=True)
            # both master and slave has it
            if env.config['exec_mode'] in ('master', 'slave'):
                close_socket(self.tapping_controller_socket, now=True)

            if 'CONTROLLER' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                    'SOS_DEBUG']:
                env.log_to_file('CONTROLLER',
                                f'controller stopped {os.getpid()}')
