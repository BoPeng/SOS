#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.
import copy
import pickle
import time
from collections import defaultdict

import fasteners
import networkx as nx

from .targets import (sos_step, sos_targets, sos_variable, BaseTarget)
from .utils import ActivityNotifier, env, short_repr, textMD5

from typing import Union
#
# DAG design:
#
# 1. Nodes are jobs to be completed currently they are the
#    same as steps.
#
# 2. Input, output and depedent files of nodes.
#
#    Each step has input, output and depdent files with their status.
#    The status of these files determines the status of ndoes.
#    There are several status for these targets
#
#    a. Target exists and without signature.
#       The files need to be processed and obtain its signature.
#
#    b. Target does not exists and with signature.
#       The files are assumed to exist for dependency check, but
#       are determined to be non-exist if they are needed for a
#       task.
#
#    c. Target does not exists and without signature.
#       The target need to be generated and obtain its signature.
#
#    d. Target is undetermined. A node with undetermined input
#       will depend on all its previous steps.
#
#  3. Status of node.
#
#    a. A node does not depend on any input file.
#       It can be executed immediately.
#
#    b. A node depends on undetermined input file.
#       It depend on all previous steps and can only be processed
#       when all its previous steps are implemented.
#
#    c. A node depends on a previous file.
#       It can only be executed when the previous step is executed.
#       This is the regular case.
#
#    d. A node depends on a file that does not exist.
#       This is problematic. An error will be raised.
#
# 4. Edges.
#
#    a. Edges determines dependencies between nodes. They are
#       generated depending on the status of the nodes.
#
#    b. Edges will be updated with the update of nodes.
#
# 5. Dynamic DAG.
#
#    a. DAG will consists of nodes and edges derived from
#       status of files.
#
#    b. Completion of a task or step will update the status of
#       files. All edges related to updated targets will be updated.
#
#    c. Addition of node is currently not considered, but should
#       be allowed.
#


class SoS_Node(object):

    def __init__(self, step_uuid: str, node_name: str,
                 wf_index: Union[int, None], node_index: Union[int, None],
                 input_targets: sos_targets, depends_targets: sos_targets,
                 output_targets: sos_targets, context: dict) -> None:
        self._step_uuid = step_uuid
        self._node_id = node_name
        self._wf_index = wf_index
        self._node_index = node_index
        self._input_targets = input_targets
        self._depends_targets = depends_targets
        self._output_targets = output_targets
        # env.logger.error('Note {}: Input: {} Depends: {} Output: {}'.format(self._node_id, self._input_targets,
        #      self._depends_targets,  self._output_targets))
        self._context = {} if context is None else copy.deepcopy(context)
        self._status = None
        # unique ID to avoid add duplicate nodes ...
        self._node_uuid = textMD5(
            pickle.dumps((step_uuid, node_name, node_index, input_targets,
                          depends_targets, output_targets, [] if context is None
                          else [(k, sorted(list(context[k])) if isinstance(
                              context[k], set) else context[k])
                                for k in sorted(context.keys())])))

    def __repr__(self):
        return self._node_id

    def show(self):
        print(
            f'{self._node_id} ({self._node_index}, {self._status}): input {short_repr(self._input_targets)}, depends {short_repr(self._depends_targets)}, output {short_repr(self._output_targets)}, context {self._context}'
        )


class SoS_DAG(nx.DiGraph):

    def __init__(self, *args, **kwargs):
        nx.DiGraph.__init__(self, *args, **kwargs)
        # all_input
        self._all_depends_files = defaultdict(list)
        self._all_output_files = defaultdict(list)
        # index of mini
        self._forward_workflow_id = 0
        # if dag has been changed
        self._dirty = True
        # with error, stop probing
        self._degraded = False

    def mark_dirty(self, dirty=True):
        self._dirty = dirty

    def dirty(self):
        return self._dirty

    def mark_degraded(self, degraded=True):
        self._degraded = degraded

    def degraded(self):
        return self._degraded

    def new_forward_workflow(self):
        self._forward_workflow_id += 1

    def num_nodes(self):
        return nx.number_of_nodes(self)

    def add_step(self,
                 step_uuid,
                 node_name,
                 node_index,
                 input_targets: sos_targets,
                 depends_targets: sos_targets,
                 output_targets: sos_targets,
                 context: dict = {}):
        if 'DAG' in env.config['SOS_DEBUG'] or 'ALL' in env.config['SOS_DEBUG']:
            env.log_to_file('DAG', f'add step {node_name}')
        node = SoS_Node(
            step_uuid, node_name,
            None if node_index is None else self._forward_workflow_id,
            node_index, input_targets, depends_targets, output_targets, context)
        if node._node_uuid in [x._node_uuid for x in self.nodes()]:
            return
        # adding a step would add a sos_step target to met the depends on sos_step
        # requirement of some steps.
        self._all_output_files[sos_step(node_name.split(' ')[0])].append(node)

        for x in input_targets:
            if node not in self._all_depends_files[x]:
                self._all_depends_files[x].append(node)
        for x in depends_targets:
            if node not in self._all_depends_files[x]:
                self._all_depends_files[x].append(node)
        for x in output_targets:
            if node not in self._all_output_files[x]:
                self._all_output_files[x].append(node)
        if context is not None:
            for x in context['__changed_vars__']:
                if node not in self._all_output_files[sos_variable(x)]:
                    self._all_output_files[sos_variable(x)].append(node)
        self.add_node(node)

    def update_step(self, node, input_targets: sos_targets,
                    output_targets: sos_targets, depends_targets: sos_targets):
        for x in input_targets:
            if node not in self._all_depends_files[x]:
                self._all_depends_files[x].append(node)
        for x in depends_targets:
            if node not in self._all_depends_files[x]:
                self._all_depends_files[x].append(node)
        for x in output_targets:
            if node not in self._all_output_files[x]:
                self._all_output_files[x].append(node)

    def find_executable(self):
        '''Find an executable node, which means nodes that has not been completed
        and has no input dependency.'''
        if 'DAG' in env.config['SOS_DEBUG'] or 'ALL' in env.config['SOS_DEBUG']:
            env.log_to_file('DAG', 'find_executable')
        for node in self.nodes():
            # if it has not been executed
            if node._status is None:
                with_dependency = False
                for edge in self.in_edges(node):
                    if edge[0]._status != 'completed':
                        with_dependency = True
                        break
                if not with_dependency:
                    return node
        # if no node could be found, let use try pending ones
        pending_jobs = [
            x for x in self.nodes() if x._status == 'signature_pending'
        ]
        if pending_jobs:
            try:
                notifier = ActivityNotifier(
                    f'Waiting for {len(pending_jobs)} pending job{"s: e.g." if len(pending_jobs) > 1 else ":"} output {short_repr(pending_jobs[0]._signature[0])} with signature file {pending_jobs[0]._signature[1] + "_"}. You can manually remove this lock file if you are certain that no other process is working on the output.'
                )
                while True:
                    for node in pending_jobs:
                        # if it has not been executed
                        lock = fasteners.InterProcessLock(node._signature[1] +
                                                          '_')
                        if lock.acquire(blocking=False):
                            lock.release()
                            node._status = None
                            return node
                    time.sleep(0.1)
            except Exception as e:
                env.logger.error(str(e))
            finally:
                notifier.stop()
        return None

    def node_by_id(self, node_uuid):
        for node in self.nodes():
            if node._node_uuid == node_uuid:
                return node
        raise RuntimeError(f'Failed to locate node with UUID {node_uuid}')

    def show_nodes(self):
        for node in self.nodes():
            node.show()
        for edge in self.edges():
            print(edge)

    def circular_dependencies(self):
        if 'DAG' in env.config['SOS_DEBUG'] or 'ALL' in env.config['SOS_DEBUG']:
            env.log_to_file('DAG', 'check circular')
        try:
            return nx.find_cycle(self)
        except Exception:
            # if there is no cycle, an exception is given
            return []

    def steps_depending_on(self, target: BaseTarget, workflow):
        if target in self._all_depends_files and self._all_depends_files[target]:
            return f", needed by '{workflow.section_by_id(self._all_depends_files[target][0]._step_uuid).step_name()}'"
        return ''

    def pending(self):
        return [x for x in self.nodes() if x._status == 'failed'
               ], [x for x in self.nodes() if x._status is None]

    def running(self):
        return [x for x in self.nodes() if x._status == 'running']

    def dangling(self, targets: sos_targets):
        '''returns
        1. missing targets, which are missing from the DAG or from the provided targets
        2. existing targets of provided target list, not in DAG
        '''
        existing = set()
        missing = set()
        if env.config['trace_existing']:
            for x in self._all_depends_files.keys():
                if x not in self._all_output_files:
                    if x.target_exists():
                        existing.add(x)
                    else:
                        missing.add(x)
        else:
            missing = set([
                x for x in self._all_depends_files.keys()
                if x not in self._all_output_files and not x.target_exists()
            ])
        for x in targets:
            if x not in self._all_output_files:
                if x.target_exists('target'):
                    existing.add(x)
                else:
                    missing.add(x)
        return missing, existing

    def regenerate_target(self, target: BaseTarget):
        if target in self._all_output_files:
            for node in self._all_output_files[target]:
                if node._status == 'completed':
                    if isinstance(target, sos_step):
                        if env.config['error_mode'] == 'ignore':
                            raise RuntimeError(
                                f'Target {target} that was failed to generate is needed to continue.'
                            )
                        else:
                            raise RuntimeError(
                                f'Completed target {target} is being re-executed. Please report this bug to SoS developers.'
                            )
                    else:
                        env.logger.info(
                            f'Re-running {node._node_id} to generate {target}')
                        node._status = None
            return True
        else:
            return False

    # def subgraph_from(self, targets: sos_targets):
    #     '''Trim DAG to keep only nodes that produce targets'''
    #     if 'DAG' in env.config['SOS_DEBUG'] or 'ALL' in env.config['SOS_DEBUG']:
    #         env.log_to_file('DAG', 'create subgraph')
    #     # first, find all nodes with targets
    #     subnodes = []
    #     for node in self.nodes():
    #         if node._output_targets.valid() and any(
    #                 x in node._output_targets for x in targets):
    #             subnodes.append(node)
    #     #
    #     ancestors = set()
    #     for node in subnodes:
    #         ancestors |= nx.ancestors(self, node)
    #     return SoS_DAG(nx.subgraph(self, subnodes + list(ancestors)))

    def build(self):
        '''Connect nodes according to status of targets'''
        # right now we do not worry about status of nodes
        # connecting the output to the input of other nodes
        #
        # NOTE: This is implemented in the least efficient way just for
        # testing. It has to be re-implemented.
        #
        # refer to http://stackoverflow.com/questions/33494376/networkx-add-edges-to-graph-from-node-attributes
        #
        # several cases triggers dependency.
        if 'DAG' in env.config['SOS_DEBUG'] or 'ALL' in env.config['SOS_DEBUG']:
            env.log_to_file('DAG', 'build DAG')
        for wf in range(self._forward_workflow_id + 1):
            indexed = [x for x in self.nodes() if x._wf_index == wf]
            indexed.sort(key=lambda x: x._node_index)

            for idx, node in enumerate(indexed):
                # 1. if a node changes context (using option alias), all later steps
                # has to rely on it.
                if node._context['__changed_vars__']:
                    for later_node in indexed[idx + 1:]:
                        if node._context['__changed_vars__'] & (
                                later_node._context['__signature_vars__']
                                | later_node._context['__environ_vars__']):
                            self.add_edge(node, later_node)

                # 2. if the input of a step is undetermined, it has to be executed
                # after all its previous steps.
                if not node._input_targets.valid() and idx > 0:
                    # if there is some input specified, it does not use default
                    # input, so the relationship can be further looked before
                    if node._input_targets.undetermined():
                        # if the input is dynamic, has to rely on previous step...
                        if 'dynamic' in node._context['__environ_vars__']:
                            self.add_edge(indexed[idx - 1], node)
                        else:
                            # otherwise let us look back.
                            for prev_node in indexed[idx - 1::-1]:
                                if node._context[
                                        '__environ_vars__'] & prev_node._context[
                                            '__changed_vars__']:
                                    self.add_edge(prev_node, node)
                    else:
                        self.add_edge(indexed[idx - 1], node)
        #
        # 3. if the input of a step depends on the output of another step
        for target, in_node in self._all_depends_files.items():
            if target not in self._all_output_files:
                continue
            # it is possible that multiple nodes satisfy the same target
            out_node = self._all_output_files[target]
            for i in in_node:
                for j in out_node:
                    if j != i:
                        self.add_edge(j, i)
        self.mark_dirty()

    def save(self, dest=None):
        if not dest:
            return

        if 'DAG' in env.config['SOS_DEBUG'] or 'ALL' in env.config['SOS_DEBUG']:
            env.log_to_file('DAG', 'save DAG')
        if not hasattr(self, 'dag_count'):
            self.last_dag = None
        #
        for x in self.nodes():
            if x._status is None:
                pass
            elif x._status == 'running':
                self.add_node(x, color='green')
            elif x._status == 'failed':
                self.add_node(x, color='red')
            elif 'pending' in x._status:
                self.add_node(x, color='yellow')
            elif x._status == 'completed':
                self.add_node(x, color='blue')
            elif x._status is not None:
                env.logger.warning(f'Unmarked step status {x._status}')

        try:
            out = nx.drawing.nx_pydot.to_pydot(self).to_string()
        except Exception as e:
            env.logger.warning(f'Failed to call to_pydot: {e}')

        if self.last_dag == out:
            return
        else:
            self.last_dag = out
        # output file name
        if hasattr(dest, 'write'):
            dest.write(out)
        else:
            with open(dest, 'a' if self.last_dag else 'w') as dfile:
                dfile.write(out)
