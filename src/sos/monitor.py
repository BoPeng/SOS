#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.
import os
import stat
import threading
import time

import psutil

from .utils import env, expand_time, format_HHMMSS
from .tasks import TaskFile


class TaskMonitor(threading.Thread):
    def __init__(
        self,
        task_id,
        monitor_interval,
        resource_monitor_interval,
        max_walltime=None,
        max_mem=None,
        max_procs=None,
        sos_dict={},
    ):
        threading.Thread.__init__(self)
        self.task_id = task_id
        self.pid = os.getpid()
        self.monitor_interval = monitor_interval
        self.resource_monitor_interval = max(
            resource_monitor_interval // monitor_interval, 1
        )
        self.daemon = True
        self.max_walltime = max_walltime
        if self.max_walltime is not None:
            self.max_walltime = expand_time(self.max_walltime)
        self.max_mem = max_mem
        self.max_procs = max_procs

        self.pulse_file = os.path.join(
            os.path.expanduser("~"), ".sos", "tasks", task_id + ".pulse"
        )
        # remove previous status file, which could be readonly if the job is killed
        if os.path.isfile(self.pulse_file):
            if not os.access(self.pulse_file, os.W_OK):
                os.chmod(self.pulse_file, stat.S_IREAD | stat.S_IWRITE)
            os.remove(self.pulse_file)
        self.sos_dict = sos_dict
        with open(self.pulse_file, "a") as pd:
            pd.write(
                "#time\tproc_cpu\tproc_mem\tchildren\tchildren_cpu\tchildren_mem\n"
            )

    def _check(self):
        current_process = psutil.Process(self.pid)
        par_cpu = current_process.cpu_percent()
        par_mem = current_process.memory_info()[0]
        ch_cpu = 0
        ch_mem = 0
        children = current_process.children(recursive=True)
        n_children = len(children)
        for child in children:
            ch_cpu += child.cpu_percent()
            ch_mem += child.memory_info()[0]
        return par_cpu, par_mem, n_children, ch_cpu, ch_mem

    def _exceed_resource(self, msg):
        err_file = os.path.join(
            os.path.expanduser("~"), ".sos", "tasks", self.task_id + ".soserr"
        )
        with open(err_file, "a") as err:
            err.write(msg + "\n")
        tf = TaskFile(self.task_id)
        tf.add_outputs()
        tf.status = "aborted"
        env.logger.warning(f"{self.task_id} ``aborted``: {msg}")
        # kill the task
        p = psutil.Process(self.pid)
        p.kill()

    def run(self):
        counter = 0
        start_time = time.time()
        while True:
            try:
                tf = TaskFile(self.task_id)
                if not tf.exists():
                    env.logger.warning(f"Task {self.task_id} ``removed``")
                    # the job should be removed
                    p = psutil.Process(self.pid)
                    p.kill()
                sts = tf.status
                if sts in ("completed", "failed"):
                    break
                if sts == "aborted" or not os.path.isfile(self.pulse_file):
                    env.logger.warning(f"Task {self.task_id} ``aborted``")
                    # the job should be killed
                    p = psutil.Process(self.pid)
                    p.kill()
                    if sts != "aborted":
                        # mark the task file
                        tf.status = "aborted"
                # most of the time we only update
                if counter % self.resource_monitor_interval:
                    os.utime(self.pulse_file, None)
                else:
                    cpu, mem, nch, ch_cpu, ch_mem = self._check()
                    if (
                        "peak_cpu" not in self.sos_dict
                        or self.sos_dict["peak_cpu"] < cpu + ch_cpu
                    ):
                        self.sos_dict["peak_cpu"] = cpu + ch_cpu
                    if (
                        "peak_mem" not in self.sos_dict
                        or self.sos_dict["peak_mem"] < mem + ch_mem
                    ):
                        self.sos_dict["peak_mem"] = mem + ch_mem

                    with open(self.pulse_file, "a") as pd:
                        pd.write(
                            f"{time.time()}\t{cpu:.2f}\t{mem}\t{nch}\t{ch_cpu}\t{ch_mem}\n"
                        )
                    if self.max_procs is not None and cpu + ch_cpu > self.max_procs:
                        self._exceed_resource(
                            f"Task {self.task_id} exits because of excessive use of procs (used {cpu + ch_cpu}, limit {self.max_procs})"
                        )
                    if self.max_mem is not None and mem + ch_mem > self.max_mem:
                        self._exceed_resource(
                            f"Task {self.task_id} exits because of excessive use of max_mem (used {mem + ch_mem}, limit {self.max_mem})"
                        )
                # walltime can be checked more frequently and does not have to wait for resource option
                elapsed = time.time() - start_time
                if self.max_walltime is not None and elapsed > self.max_walltime:
                    self._exceed_resource(
                        f"Task {self.task_id} exits because of excessive run time (used {format_HHMMSS(int(elapsed))}, limit {format_HHMMSS(self.max_walltime)})"
                    )
                time.sleep(self.monitor_interval)
                counter += 1
            except Exception as e:
                # if the process died, exit the thread
                # the warning message is usually:
                # WARNING: psutil.NoSuchProcess no process found with pid XXXXX
                # env.logger.warning(str(e))
                env.logger.debug(f"Monitor of {self.task_id} failed with message {e}")
                break



class WorkflowMonitor(threading.Thread):
    def __init__(
        self,
        workflow_id,
        monitor_interval,
        resource_monitor_interval,
        max_walltime=None,
        max_mem=None,
        max_procs=None,
        sos_dict={},
    ):
        threading.Thread.__init__(self)
        self.workflow_id = workflow_id
        self.pid = os.getpid()
        self.monitor_interval = monitor_interval
        self.resource_monitor_interval = max(
            resource_monitor_interval // monitor_interval, 1
        )
        self.daemon = True
        self.max_walltime = max_walltime
        if self.max_walltime is not None:
            self.max_walltime = expand_time(self.max_walltime)
        self.max_mem = max_mem
        self.max_procs = max_procs
        self.pulse_file = os.path.join(
            os.path.expanduser("~"), ".sos", "workflows", workflow_id + ".pulse"
        )
        # remove previous status file, which could be readonly if the job is killed
        if os.path.isfile(self.pulse_file):
            if not os.stat(self.pulse_file).st_mode & stat.S_IWUSR:
                os.chmod(self.pulse_file, stat.S_IREAD | stat.S_IWRITE)
            os.remove(self.pulse_file)
        self.sos_dict = sos_dict
        with open(self.pulse_file, "a") as pd:
            pd.write(
                "#time\tproc_cpu\tproc_mem\tchildren\tchildren_cpu\tchildren_mem\n"
            )

    def _check(self):
        current_process = psutil.Process(self.pid)
        par_cpu = current_process.cpu_percent()
        par_mem = current_process.memory_info()[0]
        ch_cpu = 0
        ch_mem = 0
        children = current_process.children(recursive=True)
        n_children = len(children)
        for child in children:
            ch_cpu += child.cpu_percent()
            ch_mem += child.memory_info()[0]
        return par_cpu, par_mem, n_children, ch_cpu, ch_mem

    def _exceed_resource(self, msg):
        err_file = os.path.join(
            os.path.expanduser("~"), ".sos", "workflows", self.workflow_id + ".soserr"
        )
        with open(err_file, "a") as err:
            err.write(msg + "\n")
        env.logger.warning(f"{self.workflow_id} ``aborted``: {msg}")
        # kill the workflow
        p = psutil.Process(self.pid)
        p.kill()

    def write(self, msg):
        with open(self.pulse_file, "a") as pd:
            pd.write(
                f"#{time.time()}\t{msg}\n"
            )

    def run(self):
        counter = 0
        start_time = time.time()
        while True:
            try:
                if not os.path.isfile(self.pulse_file) or not os.stat(self.pulse_file).st_mode & stat.S_IWUSR:
                    env.logger.warning(f"Workflow {self.workflow_id} ``aborted``")
                    # the job should be killed
                    p = psutil.Process(self.pid)
                    p.kill()
                # most of the time we only update
                if counter % self.resource_monitor_interval:
                    os.utime(self.pulse_file, None)
                else:
                    cpu, mem, nch, ch_cpu, ch_mem = self._check()
                    if (
                        "peak_cpu" not in self.sos_dict
                        or self.sos_dict["peak_cpu"] < cpu + ch_cpu
                    ):
                        self.sos_dict["peak_cpu"] = cpu + ch_cpu
                    if (
                        "peak_mem" not in self.sos_dict
                        or self.sos_dict["peak_mem"] < mem + ch_mem
                    ):
                        self.sos_dict["peak_mem"] = mem + ch_mem

                    with open(self.pulse_file, "a") as pd:
                        pd.write(
                            f"{time.time()}\t{cpu:.2f}\t{mem}\t{nch}\t{ch_cpu}\t{ch_mem}\n"
                        )
                    if self.max_procs is not None and cpu + ch_cpu > self.max_procs:
                        self._exceed_resource(
                            f"Workflow {self.workflow_id} exits because of excessive use of procs (used {cpu + ch_cpu}, limit {self.max_procs})"
                        )
                    if self.max_mem is not None and mem + ch_mem > self.max_mem:
                        self._exceed_resource(
                            f"Workflow {self.workflow_id} exits because of excessive use of max_mem (used {mem + ch_mem}, limit {self.max_mem})"
                        )
                # walltime can be checked more frequently and does not have to wait for resource option
                elapsed = time.time() - start_time
                if self.max_walltime is not None and elapsed > self.max_walltime:
                    self._exceed_resource(
                        f"Workflow {self.workflow_id} exits because of excessive run time (used {format_HHMMSS(int(elapsed))}, limit {format_HHMMSS(self.max_walltime)})"
                    )
                time.sleep(self.monitor_interval)
                counter += 1
            except Exception as e:
                # if the process died, exit the thread
                # the warning message is usually:
                # WARNING: psutil.NoSuchProcess no process found with pid XXXXX
                # env.logger.warning(str(e))
                env.logger.debug(f"Monitor of {self.workflow_id} failed with message {e}")
                break


def summarizeExecution(task_id, pulses, status="Unknown"):
    peak_cpu = 0
    accu_cpu = 0
    peak_mem = 0
    accu_mem = 0
    peak_nch = 0
    start_time = None
    end_time = None
    count = 0
    for line in pulses.splitlines():
        if line.startswith("#"):
            continue
        try:
            t, c, m, nch, cc, cm = line.split()
        except Exception as e:
            env.logger.warning(f'Unrecognized resource line "{line.strip()}": {e}')
        if start_time is None:
            start_time = float(t)
            end_time = float(t)
        else:
            end_time = float(t)
        accu_cpu += float(c) + float(cc)
        accu_mem += float(m) + float(cm)
        count += 1
        if float(c) + float(cc) > peak_cpu:
            peak_cpu = float(c) + float(cc)
        if float(m) + float(cm) > peak_mem:
            peak_mem = float(m) + float(cm)
        if int(nch) > peak_nch:
            peak_nch = int(nch)
    try:
        second_elapsed = end_time - start_time
    except Exception:
        second_elapsed = 0
    result = [
        ("status", status),
        ("task", task_id),
        ("nproc", str(peak_nch)),
        ("start", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))),
        ("end", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_time))),
        (
            "duration",
            (
                ""
                if second_elapsed < 86400
                else f'{int(second_elapsed/86400)} day{"s" if second_elapsed > 172800 else ""} '
            )
            + time.strftime("%H:%M:%S", time.gmtime(second_elapsed)),
        ),
        ("cpu_peak", f"{peak_cpu:.1f}"),
        ("cpu_avg", f"{0 if count == 0 else accu_cpu/count:.1f}"),
        ("mem_peak", f"{peak_mem/1024/1024:.1f}Mb"),
        ("mem_avg", f"{0 if count == 0 else accu_mem/1024/1024/count:.1f}Mb"),
    ]
    return "\n".join(f"{x:20s} {y}" for x, y in result)
