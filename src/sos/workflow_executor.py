#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import base64
import copy
import os
import subprocess
import sys
import time
import uuid
import zmq

from collections import defaultdict
from collections.abc import Sequence
from io import StringIO
from typing import Any, Dict, List, Optional, Union
from threading import Event

from .dag import SoS_DAG
from .eval import analyze_global_statements
from .hosts import Host
from .parser import SoS_Workflow
from .pattern import extract_pattern
from .workflow_report import render_report
from .controller import Controller, connect_controllers, disconnect_controllers, create_socket, close_socket, request_answer_from_controller, send_message_to_controller
from .section_analyzer import analyze_section
from .targets import (BaseTarget, RemovedTarget, UnavailableLock, UnknownTarget,
                      file_target, path, paths, sos_step, sos_targets,
                      invalid_target, sos_variable, textMD5, named_output)
from .utils import env, pickleable, short_repr, get_localhost_ip
from .executor_utils import prepare_env, ExecuteError
from .messages import encode_msg, decode_msg

__all__ = []

# try:
#     # https://github.com/pytest-dev/pytest-cov/issues/139
#     from pytest_cov.embed import cleanup_on_sigterm
#     cleanup_on_sigterm()
# except Exception:
#     pass


class dummy_node:
    # a dummy node object to store information of node passed
    # from nested workflow
    def __init__(self, name) -> None:
        self._name = name

    def __repr__(self):
        return self._name


class ProcInfo(object):

    def __init__(self, socket, port, step) -> None:
        self.socket = socket
        self.port = port
        self.step = step
        self._last_alive = time.time()

    def __repr__(self):
        return f'{self.step}/{self.step._status}' + (
            f' on {self.step._pending_targets}'
            if self.step._status == 'target_pending' else '')

    def set_status(self, status: str) -> None:
        self.step._status = status

    def in_status(self, status: str) -> bool:
        return self.step._status == status

    def status(self):
        return self.step._status

    def is_pending(self) -> bool:
        return self.step._status.endswith('_pending')


class ExecutionManager(object):
    '''
    Execution manager that manages sockets and corresponding steps.
    For nested workflows (dummy=True), a poller will be created.
    '''

    # this class managers workers and their status ...
    def __init__(self, name, dummy=False) -> None:
        self.workflow_name = name
        self.procs = []
        self.pool = []
        # steps sent and queued from the nested workflow
        # they will be executed in random but at a higher priority than the steps
        # on the master process.
        self.step_queue = []
        self.workflow_queue = []
        self.poller = zmq.Poller() if dummy else None
        self._dummy = dummy

    def report(self, msg=''):
        env.log_to_file(
            'EXECUTOR',
            f"{env.sos_dict['workflow_id']}({self.workflow_name}) {msg} {', '.join(str(proc) for proc in self.procs if proc is not None)}"
        )

    def add_placeholder_worker(self, runnable, socket):
        runnable._status = 'step_pending'
        self.procs.append(ProcInfo(socket=socket, port=None, step=runnable))
        self.poller.register(socket, zmq.POLLIN)

    def push_to_queue(self, runnable, spec):
        # try to avoid interference between tasks
        if 'section' in spec:
            self.step_queue.append([runnable, copy.deepcopy(spec)])
        else:
            self.workflow_queue.append([runnable, copy.deepcopy(spec)])

    def _is_next_job_blocking(self):
        return self.workflow_queue and self.workflow_queue[-1][1]['blocking']

    def send_to_worker(self):
        if not self.step_queue and not self.workflow_queue:
            return False

        env.log_to_file(
            'WORKER',
            f'Executor send work to worker, {len(self.step_queue)} steps and {len(self.workflow_queue)} workflows pending.'
        )

        # ask the controller for a worker. If the next job is blocking, we
        # ask for a separate blocking.
        #
        # NOTE: #1222
        #
        # It is possible that a port is said to be avialble since the worker have completed the
        # job but the executor has not finished working on the return value. In this case the
        # port is still used by the executor and cannot be used for new jobs. Because of this we
        # have to temporarily exclude the ports that are active in the executor from the selection.
        #
        master_port = request_answer_from_controller(
            ['worker_available',
             self._is_next_job_blocking()] +
            [x.port for x in self.procs if self.procs])
        # no worker is available
        if master_port is None:
            env.log_to_file(
                'EXECUTOR',
                f'No worker is available ({len([x.port for x in self.procs if self.procs])} ports excluded)'
            )
            return False

        runnable, spec = self.step_queue.pop(
        ) if self.step_queue else self.workflow_queue.pop()

        if 'sockets' not in spec['config']:
            spec['config']['sockets'] = {}
        spec['config']['sockets']['master_port'] = master_port
        send_message_to_controller(
            ['step' if 'section' in spec else 'workflow', spec])

        # if there is a pooled proc with the same port, let us use it to avoid re-creating a socket
        proc_with_port = [
            idx for idx, proc in enumerate(self.procs)
            if proc.port == master_port
        ]
        if proc_with_port:
            raise RuntimeError(
                f'worker {str(self.procs[proc_with_port[0]])} already uses this port {master_port}'
            )

        proc_with_port = [
            idx for idx, proc in enumerate(self.pool)
            if proc.port == master_port
        ]

        if len(proc_with_port) > 1:
            raise RuntimeError('this should not happen')
        if proc_with_port:
            self.procs.append(self.pool[proc_with_port[0]])
            self.pool.pop(proc_with_port[0])
            self.procs[-1].step = runnable
        else:
            master_socket = create_socket(env.zmq_context, zmq.PAIR,
                                          'pair socket for step worker')
            master_socket.connect(master_port)
            # we need to create a ProcInfo to keep track of the step
            self.procs.append(
                ProcInfo(socket=master_socket, port=master_port, step=runnable))
        return True

    def _num_of_procs(self):
        return len([x for x in self.procs if x is not None])

    def all_done(self) -> bool:
        return not self.step_queue and not self.workflow_queue and (
            not self.procs or all(x is None for x in self.procs))

    def all_pending(self) -> bool:
        return all(x is None or x.is_pending() for x in self.procs)

    def stop_dag(self, dag):
        dag.mark_degraded(True)
        self.step_queue = []
        self.workflow_queue = []

    def dispose(self, idx: int) -> None:
        self.poller.unregister(self.procs[idx].socket)
        close_socket(self.procs[idx].socket)
        self.procs[idx] = None

    def mark_idle(self, idx: int) -> None:
        self.pool.append(self.procs[idx])
        self.procs[idx] = None

    def cleanup(self) -> None:
        self.procs = [x for x in self.procs if x is not None]

    def terminate(self) -> None:
        for proc in self.procs + self.pool:
            if proc is None:
                continue
            close_socket(proc.socket)


class Base_Executor:
    '''This is the base class of all executor that provides common
    set up and tear functions for all executors.'''

    def __init__(self,
                 workflow: Optional[SoS_Workflow] = None,
                 args: Optional[Any] = None,
                 shared: None = None,
                 config: Optional[Dict[str, Any]] = {}) -> None:
        self.workflow = workflow
        self.local_ip = get_localhost_ip()

        # args serves as two purposes
        # in the master workflow, args is the command line argument and there is no workflow variables
        # workflow_args is already set by cmd_run but we set it again because the executor can be
        # started from tests.
        self.args = [] if args is None else args
        if '__args__' not in self.args:
            # if there is __args__, this is a nested workflow and we do not test this.
            for idx, arg in enumerate(self.args):
                wf_pars = self.workflow.parameters().keys()
                if isinstance(arg, str) and arg.startswith('--'):
                    if not wf_pars:
                        raise ValueError(
                            f'Undefined parameter {arg[2:]} for command line argument "{" ".join(args[idx:])}".'
                        )
                    pars = [arg[2:], arg[2:].replace('-', '_').split('=')[0]]
                    if arg[2:].startswith('no-'):
                        pars.extend(
                            [arg[5:], arg[5:].replace('-', '_').split('=')[0]])
                    if not any(x in wf_pars for x in pars):
                        raise ValueError(
                            f'Undefined parameter {arg[2:]} for command line argument "{" ".join(args[idx:])}". Acceptable parameters are: {", ".join(wf_pars)}'
                        )
        self.shared = {} if shared is None else shared
        env.config.update(config)
        if env.config['config_file'] is not None:
            env.config['config_file'] = os.path.abspath(
                os.path.expanduser(env.config['config_file']))
        #
        # if the executor is not called from command line, without sigmode setting
        if env.config['sig_mode'] is None:
            env.config['sig_mode'] = 'default'
        # interactive mode does not pass workflow
        self.md5 = self.calculate_md5() if self.workflow else '0'

        env.config['workflow_id'] = self.md5
        env.sos_dict.set('workflow_id', self.md5)

        if isinstance(args, dict):
            # the convention is that nested workflow passes a dictionary through args
            # but uses existing env.config['workflow_args']
            env.config['workflow_vars'] = args
        else:
            # ptherwise the args are considered to be command line args
            env.config['workflow_vars'] = {}
            env.config['workflow_args'] = [] if args is None else args
        #
        # prepare global definition and variables
        global_def, global_vars = analyze_global_statements(
            self.workflow.global_stmts)
        self.workflow.global_def = global_def
        self.workflow.global_vars = global_vars
        for section in self.workflow.sections + self.workflow.auxiliary_sections:
            section.global_def = global_def
            section.global_vars = global_vars

    def write_workflow_info(self):
        # if this is the outter most workflow, master)id should have =
        # not been set so we set it for all other workflows
        workflow_info = {
            'name': self.workflow.name,
            'start_time': time.time(),
        }
        workflow_info['command_line'] = subprocess.list2cmdline(
            [os.path.basename(sys.argv[0])] + sys.argv[1:])
        if '-m tapping' in workflow_info['command_line']:
            workflow_info['command_line'] = workflow_info[
                'command_line'][:workflow_info['command_line']
                                .index('-m tapping')]
        workflow_info['project_dir'] = os.getcwd()
        workflow_info['script'] = base64.b64encode(
            self.workflow.content.text().encode()).decode('ascii')
        workflow_info['master_id'] = env.config['master_id']
        request_answer_from_controller(['workflow_sig', 'clear'])
        send_message_to_controller(
            ['workflow_sig', 'workflow', self.md5,
             repr(workflow_info)])
        if env.config['exec_mode'] == 'slave':
            env.tapping_listener_socket.send(
                encode_msg({
                    'msg_type': 'workflow_status',
                    'data': {
                        'cell_id': env.config['slave_id'],
                        'workflow_id': self.md5,
                        'workflow_name': self.workflow.name,
                        'start_time': time.time(),
                        'status': 'running'
                    }
                }))

    def run(self,
            targets: Optional[List[str]] = None,
            mode=None) -> Dict[str, Any]:
        #
        env.zmq_context = zmq.Context()

        # if this is the executor for the master workflow, start controller
        env.config['master_id'] = self.md5
        #
        # control panel in a separate thread, connected by zmq socket
        ready = Event()
        self.controller = Controller(ready)
        self.controller.start()
        # wait for the thread to start with a signature_req saved to env.config
        ready.wait()

        connect_controllers(env.zmq_context)

        try:
            succ = True
            return self.run_as_master(targets=targets, mode=mode)
        except Exception:
            succ = False
            # 1212: when a substep worker is killed, the worker will
            # be killed but the substep workers belonging to the master sos
            # will still be alive and prevents it from qutting properly
            from .executor_utils import kill_all_subprocesses
            kill_all_subprocesses(os.getpid())
            raise
        finally:
            # end progress bar when the master workflow stops
            env.log_to_file('EXECUTOR', f'Stop controller from {os.getpid()}')
            request_answer_from_controller(['done', succ])
            env.log_to_file('EXECUTOR', 'disconntecting master')
            # if the process is failed, some workers might be killed, resulting
            # in nonresponseness from the master, and the socket context cannot
            # be killed in this case.
            self.controller.join()
            disconnect_controllers(env.zmq_context if succ else None)
            # when the run() function is called again, the controller
            # thread will be start again.
            env.config['master_id'] = None

    def calculate_md5(self) -> str:
        with StringIO() as sig:
            for step in self.workflow.sections + self.workflow.auxiliary_sections:
                sig.write(f'{step.step_name()}: {step.md5}\n')
            sig.write(f'{self.args}\n')
            return textMD5(sig.getvalue())[:16]

    def reset_dict(self) -> None:

        prepare_env(self.workflow.global_def, self.workflow.global_vars,
                    env.config['workflow_vars'])

        env.parameter_vars.clear()

        env.sos_dict.set('workflow_id', self.md5)
        env.sos_dict.set('master_id', env.config['master_id'])
        env.sos_dict.set('__step_output__', sos_targets([]))
        env.sos_dict.quick_update(self.shared)

    def analyze_auxiliary_step(self, section):
        res = analyze_section(section, analysis_type='initial')
        environ_vars = res['environ_vars']
        signature_vars = res['signature_vars']
        changed_vars = res['changed_vars']
        # parameters, if used in the step, should be considered environmental
        environ_vars |= env.parameter_vars & signature_vars

        # add shared to targets
        if res['changed_vars']:
            if 'provides' in section.options:
                if isinstance(section.options['provides'], str):
                    section.options.set('provides',
                                        [section.options['provides']])
            else:
                section.options.set('provides', [])
            #
            section.options.set(
                'provides', section.options['provides'] +
                [sos_variable(var) for var in changed_vars])

        if not res['step_output'].unspecified():
            section._autoprovides = res['step_output']

    def _build_target_map(self, sections):
        self._target_map = defaultdict(list)
        self._target_patterns = defaultdict(list)

        for step in sections:
            if not hasattr(step, '_analyzed'):
                self.analyze_auxiliary_step(step)
                step._analyzed = True
            # a step first provides sos_step
            for name, index, _ in step.names:
                self._target_map[sos_step(name)].append(step)
                if index is not None:
                    self._target_map[sos_step(f'{name}_{index}')].append(step)
            # named output
            if 'namedprovides' in step.options:
                for x in step.options['namedprovides']:
                    self._target_map[named_output(x)].append(step)
            #
            if hasattr(step, '_autoprovides'):
                for x in step._autoprovides:
                    # x must be BaseTarget (file_target or others)
                    self._target_map[x].append(step)
            #
            if 'provides' in step.options:
                patterns = step.options['provides']
                # str, BaseTarget, path
                # sos_targets, Sequence, paths
                if isinstance(patterns, str):
                    if '{' in patterns and '}' in patterns:
                        self._target_patterns[patterns].append(step)
                    else:
                        self._target_map[file_target(patterns)].append(step)
                elif isinstance(patterns, path):
                    self._target_map[file_target(patterns)].append((step, True))
                elif isinstance(patterns, sos_targets):
                    for x in patterns:
                        self._target_map[x].append(step)
                elif isinstance(patterns, paths):
                    for x in patterns:
                        self._target_map[file_target(x)].append(step)
                elif isinstance(patterns, BaseTarget):
                    self._target_map[patterns].append(step)
                elif isinstance(patterns, Sequence):
                    for pattern in patterns:
                        if isinstance(pattern, str):
                            if '{' in pattern and '}' in pattern:
                                self._target_patterns[pattern].append(step)
                            else:
                                self._target_map[file_target(pattern)].append(
                                    step)
                        elif isinstance(pattern, path):
                            self._target_map[file_target(pattern)].append(step)
                        elif isinstance(pattern, sos_targets):
                            for x in pattern:
                                self._target_map[x].append(step)
                        elif isinstance(pattern, paths):
                            for x in pattern:
                                self._target_map[file_target(x)].append(step)
                        elif isinstance(pattern, BaseTarget):
                            self._target_map[pattern].append(step)
                        else:
                            raise ValueError(
                                f'Unacceptable value for option pattern {patterns}'
                            )
                else:
                    raise ValueError(
                        f'Unacceptable value for option pattern {patterns}')

    def match(self, target: BaseTarget) -> Union[Dict[str, str], bool]:
        if not hasattr(self, '_target_map'):
            self._build_target_map(self.workflow.auxiliary_sections)
        if target in self._target_map:
            res = self._target_map[target]
            if len(res) > 1:
                self._target_map[target] = list(set(res))
                res = self._target_map[target]
            return res
        if not isinstance(target, file_target) or not self._target_patterns:
            return False
        # try pattern?
        for pattern, steps in self._target_patterns.items():
            if len(steps) > 1:
                raise RuntimeError(
                    f'Multiple steps providing the same pattern.')
            # if this is a regular string
            res = extract_pattern(pattern, [str(target)])
            if res and not any(None in x for x in res.values()):
                return [(steps[0], {x: y[0] for x, y in res.items()})]
        return False

    def resolve_dangling_targets(self,
                                 dag: SoS_DAG,
                                 targets: Optional[sos_targets] = None) -> int:
        '''Feed dangling targets with their dependncies from auxiliary steps,
        optionally add other targets'''
        resolved = 0
        while True:
            added_node = 0
            # first resolve missing
            dangling_targets = dag.dangling(targets)[0]
            if dangling_targets:
                env.log_to_file(
                    'EXECUTOR',
                    f'Resolving {dangling_targets} objects from {dag.number_of_nodes()} nodes'
                )
            # find matching steps
            # check auxiliary steps and see if any steps provides it
            remaining_targets = dangling_targets
            while remaining_targets:
                target = remaining_targets.pop()
                mo = self.match(target)
                if not mo:
                    #
                    # if no step produces the target, it is possible that it is an indexed step
                    # so the execution of its previous steps would solves the dependency
                    #
                    # find all the nodes that depends on target
                    nodes = dag._all_depends_files[target]
                    for node in nodes:
                        # if this is an index step... simply let it depends on previous steps
                        if node._node_index is not None:
                            indexed = [
                                x for x in dag.nodes()
                                if x._node_index is not None and
                                x._node_index < node._node_index and
                                not x._output_targets.valid()
                            ]
                            indexed.sort(key=lambda x: x._node_index)
                            if (not indexed
                               ) or (isinstance(target, sos_step) and not any(
                                   self.workflow.section_by_id(
                                       x._step_uuid).match(target.target_name())
                                   for x in indexed)) or (not any(
                                       x._status is None or
                                       x._status.endswith('pending')
                                       for x in indexed)):
                                # all previous status has been failed or completed...
                                raise RuntimeError(
                                    f"No rule to generate target '{target}'{dag.steps_depending_on(target, self.workflow)}."
                                )
                            if node._input_targets.valid():
                                node._input_targets = sos_targets([])
                            if node._depends_targets.valid():
                                node._depends_targets = sos_targets([])
                        else:
                            raise RuntimeError(
                                f'No step to generate target {target}{dag.steps_depending_on(target, self.workflow)}'
                            )
                    if nodes:
                        resolved += 1
                    # cannot resolve, but the step is numerically indexed so we just wait
                    continue
                if len(mo) > 1:
                    # sos_step('a') could match to step a_1, a_2, etc, in this case we are adding a subworkflow
                    if isinstance(target, sos_step):
                        # create a new forward_workflow that is different from the master one
                        # get the step names
                        sections = sorted(mo, key=lambda x: x.step_name())
                        # this is only useful for executing auxiliary steps and
                        # might interfere with the step analysis
                        env.sos_dict.pop('__default_output__', None)
                        env.log_to_file(
                            'DAG',
                            f'Adding {len(sections)} steps to resolve target {target}'
                        )

                        n_added = self.add_forward_workflow(
                            dag, sections, satisfies=target)

                        added_node += n_added
                        resolved += 1
                        # dag.show_nodes()
                        continue
                    else:
                        raise RuntimeError(
                            f'Multiple steps {", ".join(x.step_name() for x in mo)} to generate target {target}'
                        )
                #
                # m0[0] can be a tuple
                #   section, context
                # or just a section
                #   section
                if isinstance(mo[0], tuple):
                    # for auxiliary, we need to set input and output, here
                    # now, if the step does not provide any alternative (e.g. no variable generated
                    # from patten), we should specify all output as output of step. Otherwise the
                    # step will be created for multiple outputs. issue #243
                    if mo[0][1]:
                        env.sos_dict['__default_output__'] = sos_targets(target)
                    else:
                        env.sos_dict['__default_output__'] = sos_targets(
                            mo[0][0].options['provides'])
                    n_added, _ = self.add_backward_step(
                        dag,
                        section=mo[0][0],
                        context=mo[0][1] if isinstance(mo[0][1], dict) else {},
                        target=target)
                else:
                    env.sos_dict['__default_output__'] = sos_targets(target)
                    n_added, _ = self.add_backward_step(
                        dag, section=mo[0], context={}, target=target)

                added_node += n_added
                resolved += n_added
                # this could be made more efficient by removing step output directly
                # but it is inefficient to remove elements from list
                remaining_targets = dag.dangling(remaining_targets)[0]

            # for existing targets that are not in DAG
            traced = [x for x in targets if x.traced]
            if not env.config['trace_existing'] and not traced:
                if added_node == 0:
                    break
                else:
                    continue

            existing_targets = dag.dangling(
                targets)[1] if env.config['trace_existing'] else traced

            remaining_targets = existing_targets
            while remaining_targets:
                target = remaining_targets.pop()
                # now we need to build DAG for existing...
                mo = self.match(target)
                if not mo:
                    # this is ok, this is just an existing target, no one is designed to
                    # generate it.
                    continue
                if len(mo) > 1:
                    # this is not ok.
                    raise RuntimeError(
                        f'Multiple steps {", ".join(x.step_name() for x in mo)} to generate target {target}'
                    )

                if isinstance(mo[0], tuple):
                    # for auxiliary, we need to set input and output, here
                    # now, if the step does not provide any alternative (e.g. no variable generated
                    # from patten), we should specify all output as output of step. Otherwise the
                    # step will be created for multiple outputs. issue #243
                    if mo[0][1]:
                        env.sos_dict['__default_output__'] = sos_targets(target)
                    else:
                        env.sos_dict['__default_output__'] = sos_targets(
                            mo[0][0].options['provides'])
                    n_added, resolved_output = self.add_backward_step(
                        dag,
                        section=mo[0][0],
                        context=mo[0][1] if isinstance(mo[0][1], dict) else {},
                        target=target)
                else:
                    env.sos_dict['__default_output__'] = sos_targets(target)
                    n_added, resolved_output = self.add_backward_step(
                        dag, section=mo[0], context={}, target=target)

                added_node += n_added
                resolved += n_added

                # adding one step can resolve many targets #1199
                if len(resolved_output) > 1:
                    remaining_targets -= set(resolved_output.targets)
                    # set(dag.dangling(remaining_targets)[1])

            if added_node == 0:
                break
        return resolved

    def add_forward_workflow(self, dag, sections, satisfies=None):
        '''Add a forward-workflow, return number of nodes added
        '''
        dag.new_forward_workflow()

        env.log_to_file('DAG',
                        f'Adding mini-workflow with {len(sections)} sections')
        default_input: sos_targets = sos_targets([])
        for idx, section in enumerate(sections):
            #
            res = analyze_section(
                section, default_input=default_input, analysis_type='forward')

            environ_vars = res['environ_vars']
            signature_vars = res['signature_vars']
            changed_vars = res['changed_vars']
            # parameters, if used in the step, should be considered environmental
            environ_vars |= env.parameter_vars & signature_vars

            # add shared to targets
            if res['changed_vars']:
                if 'provides' in section.options:
                    if isinstance(section.options['provides'], str):
                        section.options.set('provides',
                                            [section.options['provides']])
                else:
                    section.options.set('provides', [])
                #
                section.options.set(
                    'provides', section.options['provides'] +
                    [sos_variable(var) for var in changed_vars])
            context = {
                '__signature_vars__': signature_vars,
                '__environ_vars__': environ_vars,
                '__changed_vars__': changed_vars,
                '__dynamic_depends__': res['dynamic_depends'],
                '__dynamic_input__': res['dynamic_input']
            }
            # add variables to context if they exist (e.g. global parameter #1281)
            context.update(
                env.sos_dict.clone_selected_vars(context['__signature_vars__']
                                                 | context['__environ_vars__']))

            # for nested workflow, the input is specified by sos_run, not None.
            if idx == 0:
                context['__step_output__'] = env.sos_dict['__step_output__']
            # can be the only step
            if idx == len(sections) - 1 and satisfies is not None:
                res['step_output'].extend(satisfies)

            dag.add_step(
                section.uuid,
                section.step_name(),
                idx,
                res['step_input'],
                res['step_depends'],
                res['step_output'],
                context=context)
            default_input = res['step_output']
        return len(sections)

    def add_backward_step(self, dag, section, context, target):
        # only one step, we need to process it # execute section with specified input
        #
        for k, v in context.items():
            env.sos_dict.set(k, v)

        # will become input, set to None
        env.sos_dict['__step_output__'] = sos_targets()
        #
        res = analyze_section(
            section,
            default_output=env.sos_dict['__default_output__'],
            context=context,
            analysis_type='backward')
        if isinstance(target,
                      sos_step) and target.target_name() != section.step_name():
            # sos_step target "name" can be matched to "name_10" etc so we will have to
            # ensure that the target is outputted from the "name_10" step.
            # This has been done in a more advanced case when an entire workflow is
            # added
            res['step_output'].extend(target)
        elif isinstance(target, named_output):
            # when a named_output is matched, we add all names from the step
            # to avoid the step to be added multiple times for different named_steps
            # from the same step. #1166
            res['step_output'].extend(
                [named_output(x) for x in section.options['namedprovides']])

        # now, if we are adding a step that is part of a forward-style workflow
        # (with index), and the input of the step is unspecified, the it is possible
        # that the step depends on its previous steps and we will have to add these steps
        # as well. #1206
        if res['step_input'].unspecified() and section.index is not None:
            # there should be one step with that name
            prev_steps = [
                x for x in self.workflow.auxiliary_sections
                if x.name == section.name and x.index is not None and
                x.index <= section.index
            ]
            self.add_forward_workflow(dag, prev_steps, target)
            return len(prev_steps), res['step_output']

        # add a single step
        # build DAG with input and output files of step
        env.log_to_file(
            'DAG',
            f'Adding step {res["step_name"]} with output {short_repr(res["step_output"])} to resolve target {target}'
        )

        context['__signature_vars__'] = res['signature_vars']
        context['__environ_vars__'] = res['environ_vars']
        context['__changed_vars__'] = res['changed_vars']
        context['__default_output__'] = env.sos_dict['__default_output__']
        context['__dynamic_depends__'] = res['dynamic_depends']
        context['__dynamic_input__'] = res['dynamic_input']

        # NOTE: If a step is called multiple times with different targets, it is much better
        # to use different names because pydotplus can be very slow in handling graphs with nodes
        # with identical names.
        node_name = section.step_name()
        if env.sos_dict["__default_output__"]:
            node_name += f' ({short_repr(env.sos_dict["__default_output__"])})'
        dag.add_step(
            section.uuid,
            node_name,
            None,
            res['step_input'],
            res['step_depends'],
            res['step_output'],
            context=context)
        return 1, res['step_output']

    def initialize_dag(self,
                       targets: Optional[List[str]] = [],
                       nested: bool = False) -> SoS_DAG:
        '''Create a DAG by analyzing sections statically.'''
        self.reset_dict()

        dag = SoS_DAG(name=self.md5)
        targets = sos_targets(targets)

        self.add_forward_workflow(dag, self.workflow.sections)
        #
        if self.resolve_dangling_targets(dag, targets) == 0:
            if targets:
                raise UnknownTarget(f'No step to generate target {targets}.')
        #dag.show_nodes()

        # now, there should be no dangling targets, let us connect nodes
        dag.build()

        # dag.show_nodes()
        # # trim the DAG if targets are specified
        # if targets:
        #     dag = dag.subgraph_from(targets)
        # check error
        cycle = dag.circular_dependencies()
        if cycle:
            raise RuntimeError(
                f'Circular dependency detected {cycle}. It is likely a later step produces input of a previous step.'
            )
        dag.save(env.config['output_dag'])
        return dag

    def describe_completed(self):
        # return a string to summarize completed and skipped steps, substeps, and tasks
        res = []
        # if '__subworkflow_completed__' in self.completed and self.completed['__subworkflow_completed__']:
        #    res.append(f"{self.completed['__subworkflow_completed__']} completed subworkflow{'s' if self.completed['__subworkflow_completed__'] > 1 else ''}")
        # if '__subworkflow_skipped__' in self.completed and self.completed['__subworkflow_skipped__']:
        #    res.append(f"{self.completed['__subworkflow_skipped__']} skipped subworkflow{'s' if self.completed['__subworkflow_skipped__'] > 1 else ''}")
        if '__step_completed__' in self.completed and self.completed[
                '__step_completed__']:
            res.append(
                f"{round(self.completed['__step_completed__'], 1)} completed step{'s' if self.completed['__step_completed__'] > 1 else ''}"
            )
            if '__substep_completed__' in self.completed and self.completed[
                    '__substep_completed__'] and self.completed[
                        '__substep_completed__'] != self.completed[
                            '__step_completed__']:
                res.append(
                    f"{self.completed['__substep_completed__']} completed substep{'s' if self.completed['__substep_completed__'] > 1 else ''}"
                )
        if '__step_skipped__' in self.completed and self.completed[
                '__step_skipped__']:
            res.append(
                f"{round(self.completed['__step_skipped__'], 1)} ignored step{'s' if self.completed['__step_skipped__'] > 1 else ''}"
            )
            if '__substep_skipped__' in self.completed and self.completed[
                    '__substep_skipped__'] and self.completed[
                        '__substep_skipped__'] != self.completed[
                            '__step_skipped__']:
                res.append(
                    f"{self.completed['__substep_skipped__']} ignored substep{'s' if self.completed['__substep_skipped__'] > 1 else ''}"
                )
        if '__task_completed__' in self.completed and self.completed[
                '__task_completed__']:
            res.append(
                f"{self.completed['__task_completed__']} completed task{'s' if self.completed['__task_completed__'] > 1 else ''}"
            )
        if '__task_skipped__' in self.completed and self.completed[
                '__task_skipped__']:
            res.append(
                f"{self.completed['__task_skipped__']} ignored task{'s' if self.completed['__task_skipped__'] > 1 else ''}"
            )
        if len(res) > 1:
            return ', '.join(res[:-1]) + ' and ' + res[-1]
        elif len(res) == 1:
            return res[0]
        else:
            return 'no step executed'

    def step_completed(self, res, dag, runnable):
        # this function can be called by both master step and nested step
        for k, v in res['__completed__'].items():
            self.completed[k] += v
        # if the result of the result of a step
        svar = {}
        for k, v in res.items():
            if k == '__shared__':
                svar = v
                env.sos_dict.update(v)
            else:
                env.sos_dict.set(k, v)
        #
        # set context to the next logic step.
        for edge in dag.out_edges(runnable):
            node = edge[1]
            # if node is the logical next step...
            if node._node_index is not None and runnable._node_index is not None:
                # and node._node_index == runnable._node_index + 1:
                node._context.update(
                    env.sos_dict.clone_selected_vars(
                        node._context['__signature_vars__']
                        | node._context['__environ_vars__']
                        | {'_input', '__step_output__'}))
            node._context.update(svar)
            if node._status == 'target_pending':
                if all(
                        x.target_exists('target')
                        for x in node._pending_targets):
                    # in a master node, this _socket points to the step
                    # in a nested node, this _socket points to the parent socket
                    node._socket.send(encode_msg('target_resolved'))
                    node._status = 'running'
        dag.update_step(
            runnable,
            input_targets=env.sos_dict['__step_input__'],
            output_targets=env.sos_dict['__step_output__'],
            depends_targets=env.sos_dict['__step_depends__'])
        runnable._status = 'completed'
        dag.mark_dirty()
        dag.save(env.config['output_dag'])

    def handle_dependent_target(self, dag, targets, runnable) -> int:
        total_added = 0
        resolved = 0
        while True:
            added_node = 0
            # for depending targets... they already exist but we will add
            # nodes that generates them if available.
            node_added = False
            depending_targets = dag.dangling(targets)[1]
            for target in depending_targets:
                if node_added:
                    depending_targets = dag.dangling(targets)[1]
                    node_added = False
                if target not in depending_targets:
                    continue
                mo = self.match(target)
                if not mo:
                    # this is ok, this is just an existing target, no one is designed to
                    # generate it.
                    env.logger.info(f'{target} already exists')
                    continue
                if len(mo) > 1:
                    # this is not ok.
                    raise RuntimeError(
                        f'Multiple steps {", ".join(x.step_name() for x in mo)} to generate target {target}'
                    )

                if isinstance(mo[0], tuple):
                    if mo[0][1]:
                        env.sos_dict['__default_output__'] = sos_targets(target)
                    else:
                        env.sos_dict['__default_output__'] = sos_targets(
                            mo[0][0].options['provides'])
                    n_added, _ = self.add_backward_step(
                        dag,
                        section=mo[0][0],
                        context=mo[0][1] if isinstance(mo[0][1], dict) else {},
                        target=target)
                else:
                    env.sos_dict['__default_output__'] = sos_targets(target)
                    n_added, _ = self.add_backward_step(
                        dag, section=mo[0], context={}, target=target)

                node_added = True
                added_node += n_added
                resolved += n_added

            total_added += added_node
            if added_node == 0:
                break
        #
        if total_added:
            if runnable._depends_targets.valid():
                runnable._depends_targets.extend(targets)
            for taget in targets:
                if runnable not in dag._all_depends_files[target]:
                    dag._all_depends_files[target].append(runnable)
            dag.build()
            #
            cycle = dag.circular_dependencies()
            if cycle:
                raise RuntimeError(
                    f'Circular dependency detected {cycle}. It is likely a later step produces input of a previous step.'
                )
            dag.save(env.config['output_dag'])
        return total_added

    def handle_unknown_target(self, target, dag, runnable):
        runnable._status = None
        dag.save(env.config['output_dag'])

        if isinstance(target, file_target) and (target + '.zapped').exists():
            (target + '.zapped').unlink()

        if dag.regenerate_target(target):
            runnable._depends_targets.extend(target)
            if runnable not in dag._all_depends_files[target]:
                dag._all_depends_files[target].append(runnable)

            dag.build()
            #
            cycle = dag.circular_dependencies()
            if cycle:
                raise RuntimeError(
                    f'Circular dependency detected {cycle} after regeneration. It is likely a later step produces input of a previous step.'
                )
        else:
            if self.resolve_dangling_targets(dag, sos_targets(target)) == 0:
                raise RuntimeError(
                    f'Failed to regenerate or resolve {target}{dag.steps_depending_on(target, self.workflow)}.'
                )
            if runnable._depends_targets.valid():
                runnable._depends_targets.extend(target)
            if runnable not in dag._all_depends_files[target]:
                dag._all_depends_files[target].append(runnable)
            dag.build()
            #
            cycle = dag.circular_dependencies()
            if cycle:
                raise RuntimeError(
                    f'Circular dependency detected {cycle}. It is likely a later step produces input of a previous step.'
                )
        dag.save(env.config['output_dag'])

    def handle_unavailable_lock(self, res, dag, runnable):
        runnable._status = 'signature_pending'
        dag.save(env.config['output_dag'])
        runnable._signature = (res.output, res.sig_file)
        section = self.workflow.section_by_id(runnable._step_uuid)
        env.logger.info(
            f'Waiting on another process for step {section.step_name(True)}')

    def get_shared_vars(self, option):
        if isinstance(option, str):
            svars = [option]
        elif isinstance(option, dict):
            svars = option.keys()
        elif isinstance(option, Sequence):
            svars = []
            for x in option:
                if isinstance(x, str):
                    svars.append(x)
                elif isinstance(x, dict):
                    svars.extend(x.keys())
                else:
                    raise ValueError(
                        f'Unacceptable value for parameter shared: {option}')
        else:
            raise ValueError(
                f'Unacceptable value for parameter shared: {option}')
        return {
            x: env.sos_dict[x]
            for x in svars
            if x in env.sos_dict and pickleable(env.sos_dict[x], x)
        }

    def finalize_and_report(self):
        # remove task pending status if the workflow is completed normally
        if self.workflow.name != 'scratch':
            if self.completed["__step_completed__"] == 0:
                sts = 'ignored'
            elif env.config["run_mode"] == 'dryrun':
                sts = 'tested successfully'
            else:
                sts = 'executed successfully'
            env.logger.info(
                f'Workflow {self.workflow.name} (ID={self.md5}) is {sts} with {self.describe_completed()}.'
            )
        if env.config['output_dag']:
            env.logger.info(f"Workflow DAG saved to {env.config['output_dag']}")
        workflow_info = {
            'end_time': time.time(),
            'stat': dict(self.completed),
        }
        if env.config['output_dag'] and env.config['master_id'] == self.md5:
            workflow_info['dag'] = env.config['output_dag']
        send_message_to_controller(
            ['workflow_sig', 'workflow', self.md5,
             repr(workflow_info)])
        if env.config['master_id'] == env.sos_dict[
                'workflow_id'] and env.config['output_report']:
            # if this is the outter most workflow
            render_report(env.config['output_report'],
                          env.sos_dict['workflow_id'])
        if env.config['run_mode'] == 'dryrun':
            for filename in request_answer_from_controller(
                ['workflow_sig', 'placeholders', env.sos_dict['workflow_id']]):
                try:
                    if os.path.getsize(file_target(filename)) == 0:
                        file_target(filename).unlink()
                        env.log_to_file('EXECUTOR',
                                        f'Remove placeholder {filename}')
                except Exception as e:
                    env.log_to_file(
                        'EXECUTOR',
                        f'Failed to remove placeholder {filename}: {e}')

    def run_as_master(self, targets=None, mode=None) -> Dict[str, Any]:
        self.completed = defaultdict(int)

        self.write_workflow_info()

        self.reset_dict()

        env.config['run_mode'] = env.config.get('run_mode',
                                                'run') if mode is None else mode

        # passing run_mode to SoS dict so that users can execute blocks of
        # python statements in different run modes.
        env.sos_dict.set('run_mode', env.config['run_mode'])

        wf_result = {'__workflow_id__': self.md5, 'shared': {}}

        try:
            if env.config['output_dag'] and os.path.isfile(
                    env.config['output_dag']):
                os.unlink(env.config['output_dag'])
        except Exception as e:
            env.logger.warning(
                f'Failed to remove existing DAG file {env.config["output_dag"]}: {e}'
            )

        if targets and sos_targets(
                targets).target_exists() and not env.config['trace_existing']:
            env.logger.info(
                f'Target{"s" if len(sos_targets(targets)) > 1 else ""} {sos_targets(targets)} already exists. Remove or use option "-T" if you would like to regenerate {"them" if len(targets) > 1 else "it"}.'
            )
            wf_result['__completed__'] = 0
            return wf_result

        # process step of the pipelinp
        try:
            dag = self.initialize_dag(targets=targets)
        except UnknownTarget:
            # if the names cannot be found, try to see if they are named_output
            try:
                named_targets = [named_output(x) for x in targets]
                dag = self.initialize_dag(targets=named_targets)
            except UnknownTarget:
                raise RuntimeError(f'No step to generate target {targets}')

        # manager of processes
        manager = ExecutionManager(name=self.workflow.name)
        #
        try:
            exec_error = ExecuteError(self.workflow.name)
            while True:
                # step 1: check existing jobs and see if they are completed
                for idx, proc in enumerate(manager.procs):
                    if proc is None:
                        continue

                    # echck if there is any message from the socket
                    if not proc.socket.poll(0):
                        continue

                    # receieve something from the worker
                    res = decode_msg(proc.socket.recv())
                    runnable = proc.step
                    # if this is NOT a result, rather some request for task, step, workflow etc
                    if isinstance(res, list):
                        if res[0] == 'tasks':
                            env.log_to_file(
                                'TASK', f'Master receives task request {res}')
                            host = res[1]
                            if host == '__default__':
                                if 'default_queue' in env.config:
                                    host = env.config['default_queue']
                                else:
                                    host = 'localhost'
                            try:
                                new_tasks = res[2:]
                                runnable._host = Host(host)
                                if hasattr(runnable, '_pending_tasks'):
                                    runnable._pending_tasks.extend(new_tasks)
                                else:
                                    runnable._pending_tasks = new_tasks
                                for task in new_tasks:
                                    runnable._host.submit_task(task)
                                runnable._status = 'task_pending'
                                env.log_to_file('EXECUTOR',
                                                'Step becomes task_pending')
                            except Exception as e:
                                proc.socket.send(
                                    encode_msg({
                                        x: {
                                            'ret_code': 1,
                                            'task': x,
                                            'output': {},
                                            'exception': e
                                        } for x in new_tasks
                                    }))
                                env.logger.error(str(e))
                                proc.set_status('failed')
                                if env.config['error_mode'] == 'abort':
                                    manager.stop_dag(dag)
                        elif res[0] == 'missing_target':
                            # the target that is missing from the running step
                            missed = res[1]
                            if hasattr(runnable, '_from_nested'):
                                # if the step is from a subworkflow, then the missing target
                                # should be resolved by the nested workflow
                                runnable._child_socket.send(encode_msg(res))
                                reply = decode_msg(
                                    runnable._child_socket.recv())
                                if reply:  # if the target is resolvable in nested workflow
                                    runnable._status = 'target_pending'
                                    runnable._pending_targets = [missed]
                                    # tell the step that the target is resolved and it can continue
                                    runnable._socket = proc.socket
                                else:
                                    # otherwise say the target cannot be resolved
                                    proc.socket.send(encode_msg(''))
                                    proc.set_status('failed')
                                    if env.config['error_mode'] == 'abort':
                                        manager.stop_dag(dag)
                            else:
                                # if the missing target is from master, resolve from here
                                try:
                                    self.handle_unknown_target(
                                        missed, dag, runnable)
                                    runnable._status = 'target_pending'
                                    runnable._pending_targets = [missed]
                                    runnable._socket = proc.socket
                                except Exception as e:
                                    if env.config['error_mode'] == 'ignore':
                                        env.logger.warning(str(e))
                                        proc.socket.send(encode_msg(''))
                                    else:
                                        env.logger.error(str(e))
                                        proc.socket.send(encode_msg(''))
                                        proc.set_status('failed')
                                        if env.config['error_mode'] == 'abort':
                                            manager.stop_dag(dag)
                        elif res[0] == 'dependent_target':
                            # The target might be dependent on other steps and we
                            # are trying to extend the DAG to verify the target
                            # if possible. It does not matter if the DAG cannot be
                            # extended.
                            if hasattr(runnable, '_from_nested'):
                                # if the step is from a subworkflow, then the missing target
                                # should be resolved by the nested workflow
                                runnable._child_socket.send(encode_msg(res))
                                reply = decode_msg(
                                    runnable._child_socket.recv())
                                if reply:
                                    # if there are dependent steps, the current step
                                    # has to wait
                                    runnable._status = 'target_pending'
                                    runnable._pending_targets = res[1:]
                                    # tell the step that the target is resolved and it can continue
                                    runnable._socket = proc.socket
                                else:
                                    # otherwise there is no target to verify
                                    # and we just continue
                                    proc.socket.send(
                                        encode_msg('target_resolved'))
                            else:
                                # if the missing target is from master, resolve from here
                                reply = self.handle_dependent_target(
                                    dag, sos_targets(res[1:]), runnable)
                                if reply:
                                    runnable._status = 'target_pending'
                                    runnable._pending_targets = res[1:]
                                    runnable._socket = proc.socket
                                else:
                                    proc.socket.send(
                                        encode_msg('target_resolved'))
                        elif res[0] == 'step':
                            # step sent from nested workflow
                            step_id = res[1]
                            step_params = res[2:]

                            section, context, shared, args, config, verbosity, port = step_params
                            env.log_to_file(
                                'EXECUTOR',
                                f'Master receives step request for step {section.step_name()}'
                            )
                            # run it!
                            runnable = dummy_node(section.step_name())
                            runnable._node_id = step_id
                            runnable._status = 'running'
                            runnable._from_nested = True
                            runnable._child_socket = create_socket(
                                env.zmq_context, zmq.PAIR,
                                'child socket for dummy')
                            runnable._child_socket.connect(port)

                            manager.push_to_queue(
                                runnable,
                                spec=dict(
                                    section=section,
                                    context=context,
                                    shared=shared,
                                    args=args,
                                    config=config,
                                    verbosity=verbosity))

                        elif res[0] == 'workflow':
                            workflow_ids, wfs, targets, args, shared, config, blocking = res[
                                1:]
                            # receive the real definition
                            env.log_to_file(
                                'EXECUTOR',
                                f'Master receives workflow request {workflow_ids}'
                            )

                            # now we would like to find a worker and
                            if hasattr(runnable, '_pending_workflows'):
                                runnable._pending_workflows.extend(workflow_ids)
                            else:
                                runnable._pending_workflows = workflow_ids
                            runnable._status = 'workflow_pending'
                            dag.save(env.config['output_dag'])

                            for wid, wf in zip(workflow_ids, wfs):
                                # for each subworkflow, create a dummy node to track
                                # its status. Note that this dummy_node does not have
                                # the _from_nested flag.
                                wfrunnable = dummy_node(wf.name)
                                wfrunnable._node_id = wid
                                wfrunnable._status = 'workflow_running_pending'
                                dag.save(env.config['output_dag'])
                                wfrunnable._pending_workflows = [wid]
                                #
                                manager.push_to_queue(
                                    wfrunnable,
                                    spec=dict(
                                        workflow_id=wid,
                                        wf=wf,
                                        targets=targets,
                                        args=args,
                                        shared=shared,
                                        config=config,
                                        blocking=blocking))
                        else:
                            raise RuntimeError(
                                f'Unexpected value from step {short_repr(res)}')
                        continue
                    elif isinstance(res, str):
                        raise RuntimeError(
                            f'Unexpected value from step {short_repr(res)}')

                    # when a worker receives a result, it should be done so the socket should no longer
                    # be used... until the next time the worker use the same socket for another step
                    manager.mark_idle(idx)

                    if hasattr(runnable, '_from_nested'):
                        # if the runnable is from nested, we will need to send the result back
                        # to the nested workflow
                        env.log_to_file('EXECUTOR',
                                        f'Master send res to nested')
                        manager.report()
                        dag.save(env.config['output_dag'])
                        if isinstance(res, Exception
                                     ) and env.config['error_mode'] == 'ignore':
                            env.log_to_file(
                                'EXECUTOR',
                                'master sent exception to nested {res}. Turnning to result.'
                            )
                            env.logger.warning(
                                f'Error from step {runnable} is ignored: {res}')
                            res = {
                                '__step_input__':
                                    sos_targets(),
                                '__step_output__':
                                    sos_targets(invalid_target()),
                                '__step_depends__':
                                    sos_targets(),
                                '__shared__': {},
                                '__step_name__':
                                    '',
                                '__completed__': {
                                    '__substep_completed__': 0,
                                    '__substep_skipped__': 0,
                                    '__step_completed__': 0,
                                    '__step_skipped__': 0
                                },
                            }
                        runnable._status = 'completed'
                        runnable._child_socket.send(encode_msg(res))
                        # this is a onetime use socket that passes results from
                        # nested workflow to master
                        #runnable._child_socket.LINGER = 0
                        close_socket(runnable._child_socket)
                    elif isinstance(res, UnavailableLock):
                        self.handle_unavailable_lock(res, dag, runnable)
                    elif isinstance(res, RemovedTarget):
                        self.handle_unknown_target(res.target, dag, runnable)
                    # if the job is failed
                    elif isinstance(res, Exception):
                        env.log_to_file('EXECUTOR',
                                        f'Master received an exception')
                        if runnable._status == 'workflow_running_pending':
                            for pwf in runnable._pending_workflows:
                                for midx, proc in enumerate(manager.procs):
                                    if proc is None:
                                        continue
                                    if proc.in_status(
                                            'workflow_pending'
                                    ) and pwf in proc.step._pending_workflows:
                                        proc.step._pending_workflows.remove(pwf)
                                        if not proc.step._pending_workflows:
                                            proc.set_status('failed')
                                            manager.mark_idle(midx)

                        if env.config['error_mode'] == 'ignore':
                            env.logger.warning(
                                f'Error from step {runnable} is ignored: {res}')
                            res = {
                                '__step_input__':
                                    sos_targets(),
                                '__step_output__':
                                    sos_targets(invalid_target()),
                                '__step_depends__':
                                    sos_targets(),
                                '__shared__': {},
                                '__step_name__':
                                    '',
                                '__completed__': {
                                    '__substep_completed__': 0,
                                    '__substep_skipped__': 0,
                                    '__step_completed__': 0,
                                    '__step_skipped__': 0
                                },
                            }
                            self.step_completed(res, dag, runnable)
                        elif env.config['error_mode'] == 'abort':
                            runnable._status = 'failed'
                            raise res
                        else:
                            # env.logger.error(res)
                            runnable._status = 'failed'
                            dag.save(env.config['output_dag'])
                            exec_error.append(runnable._node_id, res)
                            # stop raising exce_error immediately, which would terminates other substeps
                            # 1265
                            env.logger.debug(f'Step {runnable} failed')
                    elif '__step_name__' in res:
                        env.log_to_file(
                            'EXECUTOR',
                            f'Master receive result for step {res["__step_name__"]}'
                        )
                        self.step_completed(res, dag, runnable)
                    elif '__workflow_id__' in res:
                        # result from a workflow
                        # the worker process has been returned to the pool, now we need to
                        # notify the step that is waiting for the result
                        env.log_to_file(
                            'EXECUTOR',
                            f'Master receive result for workflow {res["__workflow_id__"]}'
                        )
                        # aggregate steps etc with subworkflows
                        for k, v in res['__completed__'].items():
                            self.completed[k] += v
                        # if res['__completed__']['__step_completed__'] == 0:
                        #    self.completed['__subworkflow_skipped__'] += 1
                        # else:
                        #    self.completed['__subworkflow_completed__'] += 1
                        for proc in manager.procs:
                            # do not care about dummy processes
                            if proc is None:
                                continue
                            if proc.in_status('workflow_pending') and res[
                                    '__workflow_id__'] in proc.step._pending_workflows:
                                proc.step._pending_workflows.remove(
                                    res['__workflow_id__'])
                                if not proc.step._pending_workflows:
                                    proc.set_status('running')
                                proc.socket.send(encode_msg(res))
                                break
                        dag.save(env.config['output_dag'])
                    else:
                        raise RuntimeError(
                            f'Unrecognized response from a step: {res}')

                # remove None
                manager.cleanup()

                # step 2: check if some jobs are done
                for proc_idx, proc in enumerate(manager.procs):
                    # if a job is pending, check if it is done.
                    if proc.in_status('task_pending'):
                        res = proc.step._host.check_status(
                            proc.step._pending_tasks)
                        # env.logger.warning(res)
                        if all(
                                x in ('completed', 'aborted', 'failed')
                                for x in res):
                            env.log_to_file(
                                'EXECUTOR',
                                f'Proc {proc_idx} puts results for {" ".join(proc.step._pending_tasks)} from step {proc.step._node_id}'
                            )
                            res = proc.step._host.retrieve_results(
                                proc.step._pending_tasks)
                            proc.socket.send(encode_msg(res))
                            proc.step._pending_tasks = []
                            proc.set_status('running')
                            #proc.set_status('failed')
                        elif any(
                                x in ('new', 'pending', 'submitted', 'running')
                                for x in res):
                            continue
                        elif 'missing' in res:
                            raise RuntimeError(
                                f'Task no longer exists: {" ".join(x for x,y in zip(proc.step._pending_tasks, res) if y == "missing")}'
                            )
                        else:
                            raise RuntimeError(
                                f'Task {" ".join(proc.step._pending_tasks)} returned with status {" ".join(res)}'
                            )
                    elif proc.in_status('target_pending') and hasattr(
                            proc.step,
                            '_from_nested') and proc.step._child_socket.poll(0):
                        # see if the child node has sent something
                        res = decode_msg(proc.step._child_socket.recv())
                        if res == 'target_resolved':
                            # this _socket is the socket to the step
                            proc.step._socket.send(encode_msg(res))
                            proc.step._status = 'running'
                        else:
                            raise RuntimeError(
                                f'Unrecognized response from child process {res}'
                            )

                # step 3: check if there is room and need for another job
                while True:
                    if not dag.dirty() or dag.degraded():
                        break
                    # find any step that can be executed and run it, and update the DAT
                    # with status.
                    runnable = dag.find_executable()
                    if runnable is None:
                        manager.report('No more executable')
                        # do not try to find executable until the dag becomes dirty again
                        dag.mark_dirty(False)
                        break

                    # find the section from runnable
                    section = self.workflow.section_by_id(runnable._step_uuid)
                    # execute section with specified input
                    runnable._status = 'running'
                    dag.save(env.config['output_dag'])

                    # workflow shared variables
                    shared = {
                        x: env.sos_dict[x]
                        for x in self.shared.keys()
                        if x in env.sos_dict and pickleable(env.sos_dict[x], x)
                    }
                    if 'shared' in section.options:
                        shared.update(
                            self.get_shared_vars(section.options['shared']))

                    if 'workflow_id' in env.sos_dict:
                        runnable._context['workflow_id'] = env.sos_dict[
                            'workflow_id']

                    env.log_to_file('EXECUTOR',
                                    f'Master execute {section.md5} from DAG')
                    manager.push_to_queue(
                        runnable,
                        spec=dict(
                            section=section,
                            context=runnable._context,
                            shared=shared,
                            args=self.args,
                            config=env.config,
                            verbosity=env.verbosity))

                while True:
                    # if steps from child nested workflow?
                    if not manager.send_to_worker():
                        break

                if manager.all_done():
                    break
                elif dag.degraded() and manager.all_pending():
                    env.log_to_file(
                        'EXECUTOR',
                        f'Master terminate because {"degration" if dag.degraded() else "" } {" all pending" if manager.all_pending() else ""}'
                    )
                    break
                else:
                    time.sleep(0.1)
        except KeyboardInterrupt:
            if exec_error.errors:
                failed_steps, pending_steps = dag.pending()
                if pending_steps:
                    sections = [
                        self.workflow.section_by_id(
                            x._step_uuid).step_name(True) for x in pending_steps
                    ]
                    exec_error.append(
                        self.workflow.name,
                        RuntimeError(
                            f'{len(sections)} pending step{"s" if len(sections) > 1 else ""}: {", ".join(sections)}'
                        ))
                    raise exec_error
            else:
                raise
        # close all processes
        except Exception as e:
            exec_error.append(self.workflow.name, e)
        finally:
            manager.terminate()
        #
        env.log_to_file(
            'EXECUTOR', f'Master workflow {self.workflow.name} completed ' +
            ('with' if exec_error.errors else 'without') + ' error')

        if exec_error.errors:
            failed_steps, pending_steps = dag.pending()
            running_steps = dag.running()

            pending_sections = [
                self.workflow.section_by_id(x._step_uuid).step_name(True)
                for x in pending_steps
            ]
            running_sections = [
                self.workflow.section_by_id(x._step_uuid).step_name(True)
                for x in running_steps
            ]
            msg = []
            if pending_sections:
                msg.append(
                    f'{len(pending_sections)} pending step{"s" if len(pending_sections) > 1 else ""} ({", ".join(pending_sections)})'
                )
            if running_sections:
                msg.append(
                    f'{len(running_sections)} running step{"s" if len(running_sections) > 1 else ""} ({", ".join(running_sections)})'
                )

            if msg:
                exec_error.append(
                    self.workflow.name,
                    RuntimeError('Exits with ' + ' and '.join(msg)))
            raise exec_error
        elif 'pending_tasks' not in wf_result or not wf_result['pending_tasks']:
            self.finalize_and_report()
        else:
            # exit with pending tasks
            pass
        wf_result['shared'] = {
            x: env.sos_dict[x] for x in self.shared.keys() if x in env.sos_dict
        }
        wf_result['__completed__'] = self.completed
        return wf_result

    def run_as_nested(self,
                      parent_socket,
                      targets=None,
                      my_workflow_id='',
                      mode=None) -> Dict[str, Any]:
        #
        # run a nested workflow, it simply send all steps and tasks to the master to execute
        #
        # this function still uses a manager, but never calls manager.execute()
        #
        self.completed = defaultdict(int)

        self.reset_dict()
        env.config['run_mode'] = env.config.get('run_mode',
                                                'run') if mode is None else mode
        env.sos_dict.set('run_mode', env.config['run_mode'])

        wf_result = {'__workflow_id__': my_workflow_id, 'shared': {}}

        env.log_to_file(
            'WORKER',
            f'- SUBSTART - Run workflow W{env.config["workflow_vars"].get("idx", "?")}'
        )

        if targets and sos_targets(
                targets).target_exists() and not env.config['trace_existing']:
            env.logger.info(
                f'Target{"s" if len(sos_targets(targets)) > 1 else ""} {sos_targets(targets)} already exists. Remove or use option "-T" if you would like to regenerate {"them" if len(targets) > 1 else "it"}.'
            )
            wf_result['__completed__'] = {}
            parent_socket.send(encode_msg(wf_result))
            return

        # this is the initial targets specified by subworkflow, users
        # should specify named_output directly if needed.
        dag = self.initialize_dag(targets=targets)

        # the mansger will have all fake executors
        manager = ExecutionManager(name=self.workflow.name, dummy=True)
        #
        try:
            exec_error = ExecuteError(self.workflow.name)
            while True:
                # if there are running or pending steps, check if there is any message from master
                if manager.procs:
                    # continue only if we get any message from any of the sockets
                    yield manager.poller
                # step 1: check existing jobs and see if they are completed
                for idx, proc in enumerate(manager.procs):
                    if proc is None:
                        continue

                    # echck if there is any message from the socket
                    if not proc.socket.poll(0):
                        continue

                    # receieve something from the pipe
                    res = decode_msg(proc.socket.recv())
                    runnable = proc.step

                    if isinstance(res, list):
                        # missing target from nested workflow
                        if res[0] == 'missing_target':
                            missed = res[1]
                            try:
                                self.handle_unknown_target(
                                    missed, dag, runnable)
                                # tell the master that the nested can resolve the target
                                proc.socket.send(encode_msg(True))
                                runnable._status = 'target_pending'
                                runnable._pending_targets = [missed]
                                # when the target is resolved, tell the parent that
                                # the target is resolved and the step can continue
                                runnable._socket = proc.socket
                            except Exception as e:
                                if env.config['error_mode'] == 'ignore':
                                    env.logger.warning(str(e))
                                else:
                                    env.logger.error(str(e))
                                # tell the master that nested cannot resolve the
                                # target so the workflow should stop
                                proc.socket.send(encode_msg(False))
                            continue
                        elif res[0] == 'dependent_target':
                            reply = self.handle_dependent_target(
                                dag, sos_targets(res[1:]), runnable)
                            proc.socket.send(encode_msg(reply))
                            if reply:
                                # tell the master that the nested can resolve the target
                                runnable._status = 'target_pending'
                                runnable._pending_targets = res[1:]
                                # when the target is resolved, tell the parent that
                                # the target is resolved and the step can continue
                                runnable._socket = proc.socket
                            continue
                        else:
                            raise RuntimeError(
                                f'Unexpected value from step {short_repr(res)}')

                    # in a nested workflow, the manager manages all dummy nodes with a fake
                    # process but real socket. When the step is done. The socket needs to be
                    # closed.
                    manager.dispose(idx)
                    if isinstance(res, UnavailableLock):
                        self.handle_unavailable_lock(res, dag, runnable)
                    elif isinstance(res, RemovedTarget):
                        # RemovedTarget is usually hanled at the step level
                        # by sending a missing-target message here. However,
                        # if the exception is raised from substep workers, it is
                        # difficult for a substep to rerun particular substep
                        # so we handle it here by rerunning the entire step
                        self.handle_unknown_target(res.target, dag, runnable)
                    # if the job is failed
                    elif isinstance(res, Exception):
                        env.log_to_file('EXECUTOR',
                                        f'Nested received an exception')
                        runnable._status = 'failed'
                        dag.save(env.config['output_dag'])
                        exec_error.append(runnable._node_id, res)
                        env.logger.debug(
                            f'Step {runnable} in subworkflow {my_workflow_id} failed'
                        )
                        if env.config['error_mode'] == 'abort':
                            manager.stop_dag(dag)
                    elif '__step_name__' in res:
                        env.log_to_file(
                            'EXECUTOR',
                            f'Nested receives result for step {res["__step_name__"]}'
                        )
                        self.step_completed(res, dag, runnable)
                    else:
                        raise RuntimeError(
                            f'Nested wokflow received an unrecognized response: {res}'
                        )

                manager.cleanup()

                # step 3: find steps to run
                while True:
                    if not dag.dirty() or dag.degraded():
                        break
                    # with status.
                    runnable = dag.find_executable()
                    if runnable is None:
                        env.log_to_file('EXECUTOR',
                                        'Nested has no submitable job')
                        manager.report()
                        dag.mark_dirty(False)
                        break

                    # find the section from runnable
                    section = self.workflow.section_by_id(runnable._step_uuid)
                    # execute section with specified input
                    runnable._status = 'running'
                    dag.save(env.config['output_dag'])

                    # workflow shared variables
                    shared = {
                        x: env.sos_dict[x]
                        for x in self.shared.keys()
                        if x in env.sos_dict and pickleable(env.sos_dict[x], x)
                    }
                    if 'shared' in section.options:
                        shared.update(
                            self.get_shared_vars(section.options['shared']))

                    if 'workflow_id' in env.sos_dict:
                        runnable._context['workflow_id'] = env.sos_dict[
                            'workflow_id']

                    # send the step to the parent
                    step_id = uuid.uuid4()
                    env.log_to_file(
                        'EXECUTOR',
                        f'Nested send step {section.step_name()} to master')

                    socket = create_socket(env.zmq_context, zmq.PAIR,
                                           'worker pair socket')
                    port = socket.bind_to_random_port(f'tcp://{self.local_ip}')
                    env.log_to_file(
                        'WORKER',
                        f'- SUBRUN - SEND STEP S{env.config["workflow_vars"].get("idx", "?")}'
                    )

                    parent_socket.send(
                        encode_msg([
                            'step', step_id, section, runnable._context, shared,
                            self.args, env.config, env.verbosity,
                            f'tcp://{self.local_ip}:{port}'
                        ]))
                    # the nested workflow also needs a step to receive result
                    manager.add_placeholder_worker(runnable, socket)

                if manager.all_done():
                    break
                # elif dag.degraded() and manager.all_pending():
                #     break
                else:
                    time.sleep(0.01)
        except KeyboardInterrupt:
            if exec_error.errors:
                failed_steps, pending_steps = dag.pending()
                if pending_steps:
                    sections = [
                        self.workflow.section_by_id(
                            x._step_uuid).step_name(True) for x in pending_steps
                    ]
                    exec_error.append(
                        self.workflow.name,
                        RuntimeError(
                            f'{len(sections)} pending step{"s" if len(sections) > 1 else ""}: {", ".join(sections)}'
                        ))
                    raise exec_error
            else:
                raise
        except Exception as e:
            exec_error.append(self.workflow.name, e)
            # manager.terminate()

        env.log_to_file(
            'EXECUTOR', f'Subworkflow {self.workflow.name} completed ' +
            ('with' if exec_error.errors else 'without') + ' error')
        if exec_error.errors:
            failed_steps, pending_steps = dag.pending()
            # if failed_steps:
            # sections = [self.workflow.section_by_id(x._step_uuid).step_name() for x in failed_steps]
            # exec_error.append(self.workflow.name,
            #    RuntimeError('{} failed step{}: {}'.format(len(sections),
            #        's' if len(sections) > 1 else '', ', '.join(sections))))
            if pending_steps:
                sections = [
                    self.workflow.section_by_id(x._step_uuid).step_name(True)
                    for x in pending_steps
                ]
                exec_error.append(
                    self.workflow.name,
                    RuntimeError(
                        f'{len(sections)} pending step{"s" if len(sections) > 1 else ""}: {", ".join(sections)}'
                    ))
            parent_socket.send(encode_msg(exec_error))
        else:
            wf_result['shared'] = {
                x: env.sos_dict[x]
                for x in self.shared.keys()
                if x in env.sos_dict
            }
            wf_result['__completed__'] = self.completed
            parent_socket.send(encode_msg(wf_result))
