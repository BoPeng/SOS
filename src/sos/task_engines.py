#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.
import concurrent.futures
import copy
import os
import subprocess
import threading
import time
from collections import OrderedDict, defaultdict

from .eval import cfg_interpolate
from .utils import env, expand_time
from .tasks import TaskFile
from .messages import encode_msg
from .targets import sos_targets


class TaskEngine(threading.Thread):

    def __init__(self, agent):
        threading.Thread.__init__(self)
        self.daemon = True
        #
        # agent is the agent that provides function
        #
        #    run_command
        #
        # to submit command, which can be a direct process call, or a call
        # on the remote server.
        #
        self.agent = agent
        self.config = agent.config
        self.alias = self.config['alias']
        self.engine_ready = threading.Event()

        # tasks that we need to wait for a while to confirm its status
        # it will be in pending until it is confirmed that either
        # it has been canceled or already running.
        self.running_pending_tasks = {}
        self.running_tasks = []
        self.pending_tasks = []
        self.submitting_tasks = {}
        self.canceled_tasks = []

        self.task_status = OrderedDict()
        self.task_info = defaultdict(dict)
        self.task_results = {}
        self.last_checked = None
        if 'status_check_interval' not in self.config:
            self.status_check_interval = 10
        else:
            self.status_check_interval = self.config['status_check_interval']
        #
        if env.config['max_running_jobs'] is not None:
            # override from command line
            self.max_running_jobs = env.config['max_running_jobs']
        elif 'max_running_jobs' in self.config:
            # queue setting
            self.max_running_jobs = self.config['max_running_jobs']
        else:
            # default
            self.max_running_jobs = min(max(os.cpu_count() // 2, 4), 24)
        env.log_to_file(
            'TASK',
            f'Using {self.max_running_jobs} concurrent jobs for task engine {self.alias}'
        )

        #
        # multiple thread job submission does not work because the threads share the
        # same namespace with variables such as sos_dict. Variables changed by
        # one thread can be changed again by another thread, which makes it unsafe
        # to submit jobs this way. Multi-processing is possible but that can be done
        # later.
        #
        self._thread_workers = concurrent.futures.ThreadPoolExecutor(
            max_workers=1)
        self._status_checker = None
        #
        if 'wait_for_task' in self.config:
            self.wait_for_task = self.config['wait_for_task']
        else:
            # default
            self.wait_for_task = True
        #
        # how to stack jobs ... currently backgroun process queue
        # allows stacking of up to 1000 tasks, but PBS queue does not
        # allow stacking.
        self.batch_size = 1
        # let us report the status of task engine from time to time
        self.last_report = time.time()

    def notify_controller(self, msg):
        if env.config['exec_mode']:
            # set cell_id to slave_id so that the frontend knows which
            # cell this task belong to
            msg['cell_id'] = env.config.get('slave_id', '')
            env.tapping_listener_socket.send(
                encode_msg({
                    'msg_type': 'task_status',
                    'data': msg
                }))

    def monitor_tasks(self, tasks=None, status=None, age=None):
        '''Start monitoring specified or all tasks'''
        self.engine_ready.wait()

        if not tasks:
            tasks = self.task_status.keys()
            missing_tasks = []
        else:
            missing_tasks = [x for x in tasks if x not in self.task_status]
            tasks = [x for x in tasks if x in self.task_status]

        # we only monitor running tasks
        with threading.Lock():
            for task in tasks:
                if self.task_status[task] in (
                        'submitted',
                        'running') and task not in self.running_tasks:
                    # these tasks will be actively monitored
                    self.running_tasks.append(task)
        #
        if age is not None:
            age = expand_time(age, default_unit='d')
        return sorted([
            (x, self.task_status[x], self.task_info[x].get(
                'data', (time.time(), None, None)))
            for x in tasks
            if (status is None or self.task_status[x] in status) and
            (age is None or (
                (age > 0 and time.time() -
                 self.task_info[x].get('date',
                                       (time.time(), None, None))[0] > age) or
                (age < 0 and time.time() -
                 self.task_info[x].get('date',
                                       (time.time(), None, None))[0] < -age)))
        ],
                      key=lambda x: -x[2][0]) + [
                          (x, 'missing', '') for x in missing_tasks
                      ]

    def get_tasks(self):
        with threading.Lock():
            pending = copy.deepcopy(self.pending_tasks +
                                    list(self.submitting_tasks.keys()))
            running = copy.deepcopy(self.running_tasks)
        return pending, running

    def run(self):
        # get all system tasks that might have been running ...
        # this will be run only once when the task engine starts
        status_output = self.query_tasks(
            check_all=True, verbosity=3, numeric_times=True)
        with threading.Lock():
            for line in status_output.split('\n'):
                if not line.strip():
                    continue
                try:
                    # return creation time, start time, and duration
                    tid, tags, ct, st, dr, tst = line.split('\t')
                    if tid.startswith('w'):
                        continue
                    # for some reason on windows there can be a \r at the end
                    self.task_status[tid] = tst.strip()
                    self.task_info[tid]['date'] = [
                        float(ct) if ct.strip() else None,
                        float(st) if st.strip() else None,
                        float(dr) if dr.strip() else None
                    ]
                    self.task_info[tid]['tags'] = tags
                except Exception as e:
                    env.logger.warning(
                        f'Unrecognized response "{line}" ({e.__class__.__name__}): {e}'
                    )
        self._last_status_check = time.time()
        self.engine_ready.set()
        while True:
            # if there are running tasks or pending tasks, we need to monitor the status of the queue
            if (self.running_tasks or self.running_pending_tasks or
                    self.pending_tasks) and time.time(
                    ) - self._last_status_check > self.status_check_interval:
                if self._status_checker is None:
                    self._status_checker = self._thread_workers.submit(
                        self.query_tasks,
                        self.running_tasks +
                        list(self.running_pending_tasks.keys()),
                        check_all=False,
                        verbosity=3,
                        numeric_times=True)
                    continue
                elif self._status_checker.running():
                    time.sleep(0.01)
                    continue
                else:
                    status_output = self._status_checker.result()
                    self._status_checker = None
                #
                for line in status_output.splitlines():
                    if not line.strip():
                        continue
                    try:
                        tid, tags, ct, st, dr, tst = line.split('\t')
                        if tid not in self.running_tasks + list(
                                self.running_pending_tasks.keys()):
                            # we keep track of status of non-related tasks to check if the job queues
                            # are overwhelmed
                            env.log_to_file(
                                'TASK',
                                f'Task {tid} removed since status check.')
                            continue
                        self.task_info[tid]['date'] = [
                            float(ct) if ct.strip() else None,
                            float(st) if st.strip() else None,
                            float(dr) if dr.strip() else None
                        ]
                        self.task_info[tid]['tags'] = tags
                        self.update_task_status(tid, tst)
                    except Exception as e:
                        env.logger.warning(
                            f'Unrecognized response "{line}" ({e.__class__.__name__}): {e}'
                        )
                self.summarize_status()
                self._last_status_check = time.time()
            else:
                # if nothing to do, sleep to avoid empty loop. This will reduce CPU usage from 100% to 0.3%
                # sleeping longer will slightly reduce CPU usage but decrease responsiveness by a bit
                time.sleep(0.01)

            if self.submitting_tasks:
                with threading.Lock():
                    submitted = []
                    for k in self.submitting_tasks:
                        if not self.submitting_tasks[k].running():
                            submitted.append(k)
                            try:
                                task_submitted = self.submitting_tasks[
                                    k].result()
                            except Exception as e:
                                from .utils import get_traceback
                                env.log_to_file(
                                    'TASK',
                                    f'failed to submit task {e}: {get_traceback()}'
                                )
                                env.logger.error(
                                    f'Failed to submit task {k}: {e}')
                                task_submitted = False
                            if task_submitted:
                                for tid in k:
                                    if tid in self.canceled_tasks:
                                        # task is canceled while being prepared
                                        self.notify_controller({
                                            'queue':
                                                self.agent.alias,
                                            'task_id':
                                                tid,
                                            'status':
                                                'aborted',
                                            'update_only':
                                                True,
                                            'tags':
                                                self.task_info[tid].get(
                                                    'tags', '')
                                        })
                                    else:
                                        self.running_tasks.append(tid)
                                        self.notify_controller({
                                            'queue':
                                                self.agent.alias,
                                            'task_id':
                                                tid,
                                            'status':
                                                'submitted',
                                            'update_only':
                                                True,
                                            'tags':
                                                self.task_info[tid].get(
                                                    'tags', '')
                                        })
                            else:
                                for tid in k:
                                    self.notify_controller({
                                        'queue':
                                            self.agent.alias,
                                        'task_id':
                                            tid,
                                        'status':
                                            'failed',
                                        'update_only':
                                            True,
                                        'tags':
                                            self.task_info[tid].get('tags', '')
                                    })
                                    self.task_status[tid] = 'failed'
                        # else:
                        #    env.log_to_file('TASK', '{} is still being submitted.'.format(k))
                    for k in submitted:
                        self.submitting_tasks.pop(k)

            if self.pending_tasks:
                num_active_tasks = sum(
                    len(x) for x in self.submitting_tasks.keys()) + len(
                        self.running_tasks)
                if num_active_tasks >= self.max_running_jobs:
                    if time.time() - self.last_report > 60:
                        self.last_report = time.time()
                        env.logger.info(
                            f'Waiting for the completion of ``{num_active_tasks}`` task{"s" if num_active_tasks > 1 else ""} before submitting ``{len(self.pending_tasks)}`` pending ones.'
                        )
                    continue

                self.last_report = time.time()
                # submit at most self.max_running_jobs - num_active_tasks tasks
                n_submitted = 0
                slot = []
                slots = []
                removed_from_pending = set()
                with threading.Lock():
                    for idx, tid in enumerate(self.pending_tasks):
                        if self.task_status[tid] == 'running':
                            # env.logger.info(f'{tid} ``runnng``')
                            removed_from_pending.add(tid)
                            continue
                        elif tid in self.canceled_tasks:
                            # the job is canceled while being prepared to run
                            removed_from_pending.add(tid)
                            # env.logger.info(f'{tid} ``canceled``')
                            continue
                        else:
                            slot.append(tid)
                            n_submitted += 1

                        if len(slot) == self.batch_size or idx == len(self.pending_tasks) - 1 \
                            or n_submitted >= self.max_running_jobs - num_active_tasks:
                            removed_from_pending.update(slot)
                            slots.append(slot)
                            slot = []

                        if n_submitted >= self.max_running_jobs - num_active_tasks:
                            break

                    if removed_from_pending:
                        self.pending_tasks = [
                            x for x in self.pending_tasks
                            if x not in removed_from_pending
                        ]

                for slot in slots:
                    # if slot full or is the last pending, submit
                    for s_tid in slot:
                        env.log_to_file(
                            'TASK',
                            f'Start submitting {s_tid} (status: {self.task_status.get(s_tid, "unknown")})'
                        )
                    self.submitting_tasks[tuple(
                        slot)] = self._thread_workers.submit(
                            self.execute_tasks, slot)

            elif self.running_tasks or self.running_pending_tasks:
                if time.time() - self.last_report > 60:
                    # if there is no  pending tasks
                    self.last_report = time.time()
                    n_running = len(self.running_tasks) + len(
                        self.running_pending_tasks)
                    env.logger.info(
                        f'Waiting for the completion of ``{n_running}`` task{"s" if n_running > 1 else ""}.'
                    )
            else:
                if time.time() - self.last_report > 60:
                    self.last_report = time.time()
                    env.log_to_file(
                        'TASK',
                        'No running or pending task. Task engine is idle.')

    def submit_task(self, task_id):
        # we wait for the engine to start
        self.engine_ready.wait()

        # submit tasks simply add task_id to pending task list
        with threading.Lock():
            # if already in
            # if task_id in self.running_tasks or task_id in self.pending_tasks:
            #    self.notify_controller('{} ``{}``'.format(task_id, self.task_status[task_id]))
            #    self.notify_controller(['new-status', task_id, self.task_status[task_id]])
            #    return self.task_status[task_id]
            #
            if task_id in self.task_status and self.task_status[task_id]:
                if self.task_status[task_id] == 'running':
                    if task_id in self.running_pending_tasks:
                        if time.time(
                        ) - self.running_pending_tasks[task_id] > 60:
                            # more than 60 seconds and is still running,
                            # it is actually a running status
                            self.running_pending_tasks.pop(task_id)
                            self.running_tasks.append(task_id)
                        else:
                            # still waiting
                            return
                    else:
                        self.running_pending_tasks[task_id] = time.time()
                    env.logger.info(f'{task_id} ``already runnng``')
                    self.notify_controller({
                        'queue': self.agent.alias,
                        'task_id': task_id,
                        'status': 'pending',
                        'update_only': False,
                        'tags': self.task_info[task_id].get('tags', '')
                    })
                    return 'running'
                # there is a case when the job is already completed (complete-old), but
                # because we do not know if the user asks to rerun (-s force), we have to
                # resubmit the job. In the case of not-rerun, the task would be marked
                # completed very soon.
                elif self.task_status[task_id] == 'completed':
                    if task_id in self.running_pending_tasks:
                        self.running_pending_tasks.pop(task_id)
                    if task_id in env.config.get('resumed_tasks', []):
                        # force re-execution, but it is possible that this task has been
                        # executed but quit in no-wait mode (or canceled by user). More
                        # importantly, the Jupyter notebook would re-run complted workflow
                        # even if it has "-s force" signature.
                        # self.notify_controller(['new-status', task_id, 'completed'])
                        env.logger.info(f'{task_id} ``resume with completed``')
                        return 'completed'
                    else:
                        env.logger.info(f'{task_id} ``re-execute completed``')
                elif self.task_status[task_id] != 'new':
                    env.logger.info(
                        f'{task_id} ``restart`` from status ``{self.task_status[task_id]}``'
                    )

            # it is no longer in running status
            if task_id in self.running_pending_tasks:
                env.logger.error(
                    f'{task_id} confirmed to be canceled, restarting')
                self.running_pending_tasks.pop(task_id)

            # self.notify_controller('{} ``queued``'.format(task_id))
            self.pending_tasks.append(task_id)
            if task_id in self.canceled_tasks:
                self.canceled_tasks.remove(task_id)
            self.task_status[task_id] = 'pending'
            try:
                self.task_info[task_id]['tags'] = TaskFile(task_id).tags
            except Exception:
                # if task file does not exist, it is ok
                pass
            self.notify_controller({
                'queue': self.agent.alias,
                'task_id': task_id,
                'status': 'pending',
                'update_only': False,
                'tags': self.task_info[task_id].get('tags', '')
            })
            return 'pending'

    def summarize_status(self):
        from collections import Counter
        statuses = Counter(self.task_status.values())
        env.logger.debug(' '.join(f'{x}: {y}' for x, y in statuses.items()))

    def check_task_status(self, task_id, unknown='pending'):
        # we wait for the engine to start
        self.engine_ready.wait()
        try:
            with threading.Lock():
                return self.task_status[task_id]
        except Exception:
            # job not yet submitted
            return unknown

    def update_task_status(self, task_id, status):
        #
        if 'TASK' in env.config['SOS_DEBUG'] or 'ALL' in env.config['SOS_DEBUG']:
            engine_sts = ""
            if task_id in self.running_tasks:
                engine_sts += "in running list"
            elif task_id in self.running_pending_tasks:
                engine_sts += "in running pending list"
            elif task_id in self.pending_tasks:
                engine_sts += "in pending list"
            elif task_id in self.submitting_tasks:
                engine_sts += "in submitting list"
            elif task_id in self.canceled_tasks:
                engine_sts += "in canceled list"
            else:
                engine_sts += "not in any list"
            env.log_to_file('TASK', f'STATUS {task_id}\t{status}\t{engine_sts}')
        #
        with threading.Lock():
            if task_id in self.canceled_tasks and status != 'aborted':
                env.logger.debug(
                    f'Task {task_id} is still not killed (status {status})')
                status = 'aborted'
            if status == 'missing':
                # if a task has become missing.... there is no task file so they cannot be rerun
                env.log_to_file('TASK', f'{task_id} becomes missing.')
                if task_id in self.running_tasks:
                    self.running_tasks.remove(task_id)
                if task_id in self.pending_tasks:
                    self.pending_tasks.remove(task_id)
                if task_id in self.running_pending_tasks:
                    self.running_pending_tasks.pop(task_id)
            else:
                if task_id not in self.task_info:
                    self.task_info[task_id]['date'] = [None, None, None]
                if task_id in self.task_status and self.task_status[
                        task_id] == status:
                    # nothing has changed.
                    self.notify_controller({
                        'queue': self.agent.alias,
                        'task_id': task_id,
                        'status': status,
                        'update_only': True,
                        'start_time': self.task_info[task_id]['date'][1],
                        'tags': self.task_info[task_id].get('tags', '')
                    })
                else:
                    if status == 'running':
                        if task_id not in self.task_info:
                            self.task_info[task_id]['date'] = [
                                time.time(), time.time(), 0
                            ]
                        elif not self.task_info[task_id]['date'][1]:
                            self.task_info[task_id]['date'][1] = time.time()
                    self.notify_controller({
                        'queue': self.agent.alias,
                        'task_id': task_id,
                        'status': status,
                        'update_only': True,
                        'start_time': self.task_info[task_id]['date'][1],
                        'tags': self.task_info[task_id].get('tags', '')
                    })
            if status == 'new':
                if task_id not in self.pending_tasks:
                    pass
                elif task_id in self.running_tasks:
                    # this should not happen
                    env.logger.warning(
                        'Task in "new" status when it is supposed to be running. Resubmitting.'
                    )
                    self.submit_task(task_id)
                if task_id in self.pending_tasks:
                    self.pending_tasks.remove(task_id)
                if task_id in self.running_pending_tasks:
                    self.running_pending_tasks.pop(task_id)

            self.task_status[task_id] = status
            if status == 'pening' and task_id not in self.pending_tasks and task_id not in self.submitting_tasks:
                self.pending_tasks.append(task_id)
            if status == 'running' and task_id not in self.running_tasks:
                self.running_tasks.append(task_id)
            # terminal states, remove tasks from task list
            if status in ('completed', 'failed',
                          'aborted') and task_id in self.running_tasks:
                self.running_tasks.remove(task_id)
                if status in ('completed', 'failed'
                             ) and task_id in self.running_pending_tasks:
                    self.running_pending_tasks.pop(task_id)
                # status changed to completed
                self.task_results[task_id] = self._thread_workers.submit(
                    self.agent.receive_result,
                    task_id)
            # for running pending tasks
            if status == 'aborted' and task_id in self.running_pending_tasks:
                self.pending_tasks.append(task_id)
                self.task_status[task_id] = 'pending'
                self.running_pending_tasks.pop(task_id)

    def get_results(self, task_ids):
        res = {}
        while True:
            with threading.Lock():
                for task_id in task_ids:
                    if task_id in self.task_results:
                        if self.task_results[task_id].running():
                            time.sleep(0.1)
                        else:
                            res[task_id] = self.task_results[task_id].result()
                    elif task_id in self.task_status:
                        if self.task_status[task_id] in ('running', 'pending', 'submitted'):
                            time.sleep(0.1)
                        else:
                            res[task_id] = {
                                'task': task_id,
                                'exception': ValueError(f'Task {task_id} returns status {self.task_status[task_id]}'),
                                'ret_code': 1,
                                'output': sos_targets()
                            }
                    else:
                        res[task_id] = {
                                'task': task_id,
                                'exception': ValueError(f'Missing task {task_id}'),
                                'ret_code': 1,
                                'output': sos_targets()
                        }
            if len(res) == len(task_ids):
                return res

    def query_tasks(self,
                    tasks=None,
                    check_all=False,
                    verbosity=1,
                    html=False,
                    numeric_times=False,
                    age=None,
                    tags=None,
                    status=None):
        try:
            return self.agent.check_output(
                "{} status {} -v {} {} {} {} {} {} {}".format(
                    self.agent.config.get('sos', 'sos'),
                    '' if tasks is None else ' '.join(tasks),
                    verbosity,
                    '--all tasks' if check_all else '',
                    '--html' if html else '',
                    '--numeric-times' if numeric_times else '',
                    f'--age {age}' if age else '',
                    f'--tags {" ".join(tags)}' if tags else '',
                    f'--status {" ".join(status)}' if status else '',
                ))
        except subprocess.CalledProcessError as e:
            if verbosity >= 3:
                env.logger.warning(
                    f'Failed to query status of tasks on {self.alias}: {"" if e.stderr is None else e.stderr.decode()}'
                )
            return ''

    def kill_tasks(self, tasks, tags=None, all_tasks=False):
        # we wait for the engine to start
        self.engine_ready.wait()
        if all_tasks:
            tasks = self.pending_tasks + \
                list(self.submitting_tasks.keys()) + self.running_tasks

        for task in tasks:
            with threading.Lock():
                self.task_status[task] = 'aborted'
        for task in tasks:
            with threading.Lock():
                if task in self.pending_tasks:
                    self.pending_tasks.remove(task)
                    env.logger.debug(f'Cancel pending task {task}')
                elif task in self.submitting_tasks:
                    # this is more troublesome because the task is being
                    # submitted at a new thread.
                    env.logger.debug(f'Cancel submission of task {task}')
                elif task in self.running_tasks:
                    # the job will be killed from command line with
                    # status updated here
                    env.logger.debug(f'Killing running task {task}')
                else:
                    # it is not in the system, so we need to know what the
                    # status of the task before we do anything...
                    pass

        self.canceled_tasks.extend(tasks)
        #
        # verbosity cannot be send to underlying command because task engines
        # rely on the output of certain verbosity (-v1) to post kill the jobs
        cmd = "{} kill {} {} {}".format(
            self.agent.config.get('sos',
                                  'sos'), '' if all_tasks else ' '.join(tasks),
            f'--tags {" ".join(tags)}' if tags else '',
            '--all tasks' if all_tasks else '')

        try:
            ret = self.agent.check_output(cmd)
            env.logger.debug(f'"{cmd}" executed with response "{ret}"')
        except subprocess.CalledProcessError:
            env.logger.error('Failed to kill all tasks' if all_tasks else
                             f'Failed to kill tasks {" ".join(tasks)}')
            return ''
        return ret

    def execute_tasks(self, task_ids):
        # we wait for the engine to start
        self.engine_ready.wait()
        # this is base class, the derived class will actually submit the tasks
        for task_id in task_ids:
            if not self.agent.prepare_task(task_id):
                return False
        return True

    def purge_tasks(self,
                    tasks,
                    purge_all=False,
                    age=None,
                    status=None,
                    tags=None,
                    verbosity=2):
        try:
            # if not tasks and not purge_all:
            #     # if not --all and no task is specified, find all tasks in the current directory
            #     from .signatures import WorkflowSignatures
            #     workflow_signatures = WorkflowSignatures()
            #     tasks = [
            #         x for x in workflow_signatures.tasks() if os.path.isfile(
            #             os.path.join(
            #                 os.path.expanduser('~'), '.sos', 'tasks', x +
            #                 '.task'))
            #     ]
            return self.agent.check_output(
                "{} purge {} {} {} {} {} -v {}".format(
                    self.agent.config.get('sos', 'sos'), ' '.join(tasks),
                    '--all' if purge_all else '',
                    f'--age {age}' if age is not None else '',
                    f'--status {" ".join(status)}' if status is not None else
                    '', f'--tags {" ".join(tags)}' if tags is not None else '',
                    verbosity))
        except subprocess.CalledProcessError:
            env.logger.error(f'Failed to purge tasks {tasks}')
            return ''


class BackgroundProcess_TaskEngine(TaskEngine):

    def __init__(self, agent):
        super(BackgroundProcess_TaskEngine, self).__init__(agent)
        self.wait_for_task = False
        if 'task_template' in self.config:
            self.task_template = self.config['task_template'].replace(
                '\r\n', '\n')
        elif 'job_template' in self.config:
            env.logger.warning(
                'job_template for host configuration is deprecated. Please use task_template instead.'
            )
            self.task_template = self.config['job_template'].replace(
                '\r\n', '\n')
        else:
            self.task_template = None
        #
        if 'batch_size' in self.config:
            self.batch_size = self.config['batch_size']
        else:
            # default allow stacking of up to 1000 jobs
            self.batch_size = 1000

    def execute_tasks(self, task_ids):
        if not super(BackgroundProcess_TaskEngine,
                     self).execute_tasks(task_ids):
            env.log_to_file('TASK', f'Failed to prepare task {task_ids}')
            return False
        if self.task_template:
            if not self._submit_task_with_template(task_ids):
                return False
        else:
            if not self._submit_task(task_ids):
                return False
        return True

    def _submit_task(self, task_ids):
        # if no template, use a default command
        cmd = f"sos execute {' '.join(task_ids)} -v {env.verbosity} -s {env.config['sig_mode']} -m {env.config['run_mode']}"
        env.log_to_file('TASK',
                        f'Execute "{cmd}" (waiting={self.wait_for_task})')
        self.agent.run_command(cmd, wait_for_task=self.wait_for_task)
        return True

    def _submit_task_with_template(self, task_ids):
        '''Submit tasks by interpolating a shell script defined in task_template'''
        runtime = self.config
        runtime.update({
            'workdir': os.getcwd(),
            'cur_dir': os.getcwd(),  # for backward compatibility
            'verbosity': env.verbosity,
            'sig_mode': env.config.get('sig_mode', 'default'),
            'run_mode': env.config.get('run_mode', 'run'),
            'home_dir': os.path.expanduser('~')
        })
        if '_runtime' in env.sos_dict:
            runtime.update(env.sos_dict['_runtime'])
        if 'nodes' not in runtime:
            runtime['nodes'] = 1
        if 'cores' not in runtime:
            runtime['cores'] = 1

        # let us first prepare a task file
        job_text = ''
        for task_id in task_ids:
            runtime['task'] = task_id
            tf = TaskFile(task_id)
            task_runtime = tf.runtime
            task_runtime['_runtime'].update(
                tf.params.sos_dict.get('_runtime', {}))

            runtime[
                'command'] = f"{self.config.get('sos', 'sos')} execute {task_id} -v {env.verbosity} -s {env.config.get('sig_mode', 'default')} -m {env.config.get('run_mode', 'run')}"
            runtime.update(task_runtime['_runtime'])
            try:
                job_text += cfg_interpolate(self.task_template, runtime)
                job_text += '\n'
            except Exception as e:
                raise ValueError(
                    f'Failed to generate job file for task {task_id}: {e}')

        filename = task_ids[0] + ('.sh' if len(task_ids) == 1 else
                                  f'-{task_ids[-1]}.sh')
        # now we need to write a job file
        job_file = os.path.join(
            os.path.expanduser('~'), '.sos', 'tasks', filename)
        # do not translate newline under windows because the script will be executed
        # under linux/mac
        with open(job_file, 'w', newline='') as job:
            job.write(job_text)

        # then copy the job file to remote host if necessary
        self.agent.send_job_file(job_file)

        try:
            cmd = f'bash ~/.sos/tasks/{filename}'
            env.log_to_file('TASK', f'Execute "{cmd}" with script {job_text}')
            self.agent.run_command(cmd, wait_for_task=self.wait_for_task)
        except Exception as e:
            raise RuntimeError(f'Failed to submit task {task_ids}: {e}')
        return True
