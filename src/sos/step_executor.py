#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import ast
import copy
import os
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import List

import zmq

from .controller import close_socket, create_socket, send_message_to_controller
from .messages import encode_msg, decode_msg
from .eval import SoS_eval, SoS_exec, accessed_vars, KeepOnlyImportAndDefine
from .executor_utils import (
    __named_output__,
    __null_func__,
    __output_from__,
    __traced__,
    clear_output,
    create_task,
    get_traceback_msg,
    reevaluate_output,
    statementMD5,
    validate_step_sig,
    verify_input,
    ExecuteError,
)
from .syntax import (
    SOS_DEPENDS_OPTIONS,
    SOS_INPUT_OPTIONS,
    SOS_OUTPUT_OPTIONS,
    SOS_TARGETS_OPTIONS,
)
from .targets import (
    RemovedTarget,
    RuntimeInfo,
    UnavailableLock,
    sos_variable,
    UnknownTarget,
    dynamic,
    file_target,
    sos_step,
    sos_targets,
    invalid_target
)
from .tasks import MasterTaskParams, TaskFile
from .utils import (
    ArgumentError,
    StopInputGroup,
    TerminateExecution,
    env,
    get_traceback,
    short_repr,
    ProcessKilled,
    get_localhost_ip,
    textMD5,
)

__all__: List = []


class TaskManager:
    # manage tasks created by the step
    def __init__(self, num_tasks, trunk_size, trunk_workers):
        super(TaskManager, self).__init__()
        self.num_tasks = num_tasks
        import math

        self._slots = [[] for x in range(math.ceil(num_tasks / trunk_size))]
        self._last_slot_size = (
            trunk_size if (num_tasks % trunk_size == 0) else (num_tasks % trunk_size)
        )
        self.trunk_size = trunk_size
        self.trunk_workers = trunk_workers

        self._submitted_tasks = []
        # entire groups
        self._unsubmitted_slots = []
        # collection of partial groups if some tasks are completed
        self._unsubmitted_tasks = []
        # derived from _unsubmitted_slots
        self._all_ids = []
        self._all_output = []
        #
        self._terminate = False
        #
        self._tags = {}

    def set(self, idx, task_def):
        slot = idx // self.trunk_size
        #
        # slot [
        #   [idx, None] <- for empty
        #   [idx, taskdef] <- for non empty
        #  ]
        self._slots[slot].append([idx, task_def])
        # the slot is full
        if len(self._slots[slot]) == self.trunk_size or (
            slot == len(self._slots) - 1
            and len(self._slots[slot]) == self._last_slot_size
        ):
            # if there are valida tasks
            if not all([x[1] is None for x in self._slots[slot]]):
                # remove empty tasks and sort by id
                if self.trunk_size == 1 or any(x[1] is None for x in self._slots[slot]):
                    # if partial, sent to partial list
                    self._unsubmitted_tasks.extend(
                        [x[1] for x in self._slots[slot] if x[1] is not None]
                    )
                else:
                    self._unsubmitted_slots.append(
                        sorted(self._slots[slot], key=lambda x: x[0])
                    )
            # clear skit
            self._slots[slot] = []
        if not task_def:
            return
        if isinstance(task_def[2], Sequence):
            self._all_output.extend(task_def[2])
        self._all_ids.append(task_def[0])
        self._tags[task_def[0]] = task_def[1].tags

    def tags(self, task_id):
        return self._tags.get(task_id, [])

    def index_of(self, task_id):
        if task_id in self._all_ids:
            return self._all_ids.index(task_id)
        else:
            return -1

    def has_output(self, output):
        if not isinstance(output, Sequence) or not self._unsubmitted_slots:
            return False
        return any(x in self._all_output for x in output)

    def get_job(self, all_tasks=False):
        # single tasks
        ids = []
        # submit all tasks without trunk, easy
        for slot in self._unsubmitted_slots:
            # create a master task
            master = MasterTaskParams(self.trunk_workers)
            for _, (task_id, taskdef, _) in slot:
                master.push(task_id, taskdef)
            ids.append(master.ID)
            TaskFile(master.ID).save(master.finalize())
            send_message_to_controller(
                [
                    "workflow_sig",
                    "task",
                    master.ID,
                    f"{{'creation_time': {time.time()}}}",
                ]
            )
        self._unsubmitted_slots = []

        # individual tasks...
        if self.trunk_size == 1 or all_tasks:
            to_be_submitted = self._unsubmitted_tasks
            [
                to_be_submitted.extend([x[1] for x in slot if x[1] is not None])
                for slot in self._slots
                if slot
            ]
            self._unsubmitted_tasks = []
        else:
            # save complete blocks
            num_tasks = (
                len(self._unsubmitted_tasks) // self.trunk_size * self.trunk_size
            )
            to_be_submitted = self._unsubmitted_tasks[:num_tasks]
            self._unsubmitted_tasks = self._unsubmitted_tasks[num_tasks:]

        if self.trunk_size == 1 or (all_tasks and len(self._unsubmitted_tasks) == 1):
            for task_id, taskdef, _ in to_be_submitted:
                # if the task file, perhaps it is already running, we do not change
                # the task file. Otherwise we are changing the status of the task
                TaskFile(task_id).save(taskdef)
                send_message_to_controller(
                    [
                        "workflow_sig",
                        "task",
                        task_id,
                        f"{{'creation_time': {time.time()}}}",
                    ]
                )
                ids.append(task_id)
        else:
            master = None
            for task_id, taskdef, _ in to_be_submitted:
                if master is not None and master.num_tasks() == self.trunk_size:
                    ids.append(master.ID)
                    TaskFile(master.ID).save(master)
                    send_message_to_controller(
                        [
                            "workflow_sig",
                            "task",
                            master.ID,
                            f"{{'creation_time': {time.time()}}}",
                        ]
                    )
                    master = None
                if master is None:
                    master = MasterTaskParams(self.trunk_workers)
                master.push(task_id, taskdef)
            # the last piece
            if master is not None:
                TaskFile(master.ID).save(master.finalize())
                send_message_to_controller(
                    [
                        "workflow_sig",
                        "task",
                        master.ID,
                        f"{{'creation_time': {time.time()}}}",
                    ]
                )
                ids.append(master.ID)

        if not ids:
            return None

        self._submitted_tasks.extend(ids)
        return ids

    def clear_submitted(self):
        self._submitted_tasks = []


def expand_input_files(*args, **kwargs):
    # if unspecified, use __step_output__ as input (default)
    # resolve dynamic input.
    args = [x.resolve() if isinstance(x, dynamic) else x for x in args]
    kwargs = {
        x: (y.resolve() if isinstance(y, dynamic) else y) for x, y in kwargs.items()
    }

    # if no input,
    if not args and not kwargs:
        return env.sos_dict["step_input"]
    # if only group_by ...
    elif not args and all(x in SOS_TARGETS_OPTIONS for x in kwargs.keys()):
        return sos_targets(
            env.sos_dict["step_input"],
            _verify_existence=env.config["error_mode"] != "ignore",
            **kwargs,
        )
    else:
        return sos_targets(
            *args,
            **kwargs,
            _verify_existence=env.config["error_mode"] != "ignore",
            _undetermined=False,
            _source=env.sos_dict["step_name"],
        )


def expand_depends_files(*args, **kwargs):
    """handle directive depends"""
    args = [x.resolve() if isinstance(x, dynamic) else x for x in args]
    kwargs = {
        x: (y.resolve() if isinstance(y, dynamic) else y) for x, y in kwargs.items()
    }
    return sos_targets(
        *args,
        **kwargs,
        _verify_existence=True,
        _undetermined=False,
        _source=env.sos_dict["step_name"],
    )


def expand_output_files(value, *args, **kwargs):
    """Process output files (perhaps a pattern) to determine input files."""
    if any(isinstance(x, dynamic) for x in args) or any(
        isinstance(y, dynamic) for y in kwargs.values()
    ):
        return sos_targets(_undetermined=value)
    else:
        return sos_targets(
            *args, **kwargs, _undetermined=False, _source=env.sos_dict["step_name"]
        )


def parse_shared_vars(option):
    shared_vars = set()
    if not option:
        return shared_vars
    if isinstance(option, str):
        shared_vars.add(option)
    elif isinstance(option, Mapping):
        for val in option.values():
            shared_vars |= accessed_vars(val, mode="eval")
    elif isinstance(option, Sequence):
        for item in option:
            if isinstance(item, str):
                shared_vars.add(item)
            elif isinstance(item, Mapping):
                for val in item.values():
                    shared_vars |= accessed_vars(val, mode="eval")
    return shared_vars


def evaluate_shared(vars, option):
    # handle option shared and store variables in a "__shared_vars" variable
    shared_vars = {}
    env.sos_dict.quick_update(vars[-1])
    for key in vars[-1].keys():
        try:
            if key in ("output", "depends", "input"):
                env.logger.warning(
                    f"Cannot overwrite variable step_{key} from substep variable {key}"
                )
            else:
                env.sos_dict.set("step_" + key, [x[key] for x in vars])
        except Exception as e:
            env.logger.warning(f"Failed to create step level variable step_{key}: {e}")
    if isinstance(option, str):
        if option in env.sos_dict:
            shared_vars[option] = env.sos_dict[option]
        else:
            raise RuntimeError(f"shared variable does not exist: {option}")
    elif isinstance(option, Mapping):
        for var, val in option.items():
            try:
                if var == val:
                    shared_vars[var] = env.sos_dict[var]
                else:
                    shared_vars[var] = SoS_eval(val)
            except Exception as e:
                raise RuntimeError(
                    f"Failed to evaluate shared variable {var} from expression {val}: {e}"
                )
    # if there are dictionaries in the sequence, e.g.
    # shared=['A', 'B', {'C':'D"}]
    elif isinstance(option, Sequence):
        for item in option:
            if isinstance(item, str):
                if item in env.sos_dict:
                    shared_vars[item] = env.sos_dict[item]
                else:
                    raise RuntimeError(f"shared variable does not exist: {option}")
            elif isinstance(item, Mapping):
                for var, val in item.items():
                    try:
                        if var == val:
                            continue
                        else:
                            shared_vars[var] = SoS_eval(val)
                    except Exception as e:
                        raise RuntimeError(
                            f"Failed to evaluate shared variable {var} from expression {val}: {e}"
                        )
            else:
                raise RuntimeError(
                    f"Unacceptable shared option. Only str or mapping are accepted in sequence: {option}"
                )
    else:
        raise RuntimeError(
            f"Unacceptable shared option. Only str, sequence, or mapping are accepted in sequence: {option}"
        )
    return shared_vars


def get_value_of_param(name, param_list, extra_dict={}):
    tree = ast.parse(f"__null_func__({param_list})")
    # x.func can be an attribute (e.g. a.b()) and do not have id
    kwargs = [
        x for x in ast.walk(tree) if x.__class__.__name__ == "keyword" and x.arg == name
    ]
    if not kwargs:
        return []
    try:
        return [ast.literal_eval(kwargs[0].value)]
    except Exception:
        return [
            eval(
                compile(
                    ast.Expression(body=kwargs[0].value),
                    filename="<string>",
                    mode="eval",
                ),
                extra_dict,
            )
        ]


def is_sos_run_the_only_last_stmt(stmt):
    tree = ast.parse(stmt)
    return (
        len(tree.body) >= 1
        and isinstance(tree.body[-1], ast.Expr)
        and isinstance(tree.body[-1].value, ast.Call)
        and hasattr(tree.body[-1].value.func, "id")
        and tree.body[-1].value.func.id == "sos_run"
        and len(
            [
                x
                for x in ast.walk(tree)
                if isinstance(x, ast.Call)
                and hasattr(x.func, "id")
                and x.func.id == "sos_run"
            ]
        )
        == 1
    )


class Base_Step_Executor:
    # This base class defines how steps are executed. The derived classes will reimplement
    # some function to behave differently in different modes.
    #
    def __init__(self, step):
        self.step = step
        self.task_manager = None
        self.exec_error = ExecuteError(self.step.step_name())

    #
    #  Functions that should be redefined in derived class
    #

    def submit_tasks(self, tasks):
        raise RuntimeError("Undefined base function submit_tasks")

    def wait_for_tasks(self, tasks, all_submitted):
        # this will be redefined in subclasses
        raise RuntimeError("Undefined base function wait_for_tasks")

    def wait_for_subworkflows(self, allow_pending=0):
        raise RuntimeError("Undefined base function wait_for_subworkflows")

    def handle_unknown_target(self, e):
        raise RuntimeError("Undefined base function handle_unknown_target")

    def init_input_output_vars(self):
        # if there is __step_output__ from previous step, use it as default input
        # otherwise, reset to empty
        if (
            "__step_output__" not in env.sos_dict
            or env.sos_dict["__step_output__"].unspecified()
        ):
            env.sos_dict.set("step_input", sos_targets([]))
        else:
            env.sos_dict.set("step_input", env.sos_dict["__step_output__"])
        # input can be Undetermined from undetermined output from last step
        env.sos_dict.set("_input", copy.deepcopy(env.sos_dict["step_input"]))

        # if there is default output for auxiliary steps, use it as step_output and _output
        # otherwise reset to unspecified.
        if "__default_output__" in env.sos_dict:
            # if step is triggered by sos_step, it should not be considered as
            # output of the step. #981
            env.sos_dict.set(
                "__default_output__",
                sos_targets(
                    [
                        x
                        for x in env.sos_dict["__default_output__"]._targets
                        if not isinstance(x, sos_step)
                    ]
                ),
            )
            env.sos_dict.set(
                "step_output", copy.deepcopy(env.sos_dict["__default_output__"])
            )
            env.sos_dict.set(
                "_output", copy.deepcopy(env.sos_dict["__default_output__"])
            )
        else:
            env.sos_dict.set("step_output", sos_targets([]))
            # output is said to be unspecified until output: is used
            env.sos_dict.set("_output", sos_targets(_undetermined=True))

        env.sos_dict.set("step_depends", sos_targets([]))
        env.sos_dict.set("_depends", sos_targets([]))

    #
    # Common functions
    #

    def verify_output(self):
        missing = sos_targets([])
        if env.sos_dict["step_output"] is None:
            return
        if not env.sos_dict["step_output"].valid():
            raise RuntimeError(
                "Output of a completed step cannot be undetermined or unspecified."
            )
        for target in env.sos_dict["step_output"]:
            if isinstance(target, (sos_step, invalid_target)):
                continue
            if isinstance(target, str):
                if not file_target(target).target_exists("any"):
                    if env.config["run_mode"] == "dryrun":
                        # in dryrun mode, we just create these targets
                        file_target(target).create_placeholder()
                    else:
                        # latency wait for 2 seconds because the file system might be slow
                        if env.config["run_mode"] == "run":
                            time.sleep(2)
                        if not file_target(target).target_exists("any"):
                            if env.config["error_mode"] == "ignore":
                                missing.extend(target)
                            else:
                                raise RuntimeError(
                                    f'Output target {target} does not exist after the completion of step {env.sos_dict["step_name"]} (curdir={os.getcwd()})'
                                )
            elif not target.target_exists("any"):
                if env.config["run_mode"] == "dryrun":
                    target.create_placeholder()
                else:
                    if env.config["run_mode"] == "run":
                        time.sleep(2)
                    if not target.target_exists("any"):
                        if env.config["error_mode"] == "ignore":
                            missing.extend(target)
                        else:
                            raise RuntimeError(
                                f'Output target {target} does not exist after the completion of step {env.sos_dict["step_name"]}'
                            )
        return missing

    # directive input
    def process_input_args(self, ifiles: sos_targets, **kwargs):
        """This function handles directive input and all its parameters.
        It
            determines and set __step_input__
            determines and set pattern variables if needed
        returns
            _groups
            _vars
        which are groups of _input and related _vars
        """
        if ifiles.unspecified():
            env.sos_dict.set("step_input", sos_targets([]))
            env.sos_dict.set("_input", sos_targets([]))
            env.sos_dict.set("step_output", sos_targets())
            return [sos_targets([])], [{}]

        assert isinstance(ifiles, sos_targets)

        if env.sos_dict.get("__dynamic_input__", False):
            runner = self.verify_dynamic_targets(
                [x for x in ifiles if isinstance(x, file_target)]
            )
            try:
                yreq = next(runner)
                while True:
                    yres = yield yreq
                    yreq = runner.send(yres)
            except StopIteration:
                pass

        # input file is the filtered files
        env.sos_dict.set("step_input", ifiles)
        env.sos_dict.set("_input", ifiles)

        if ifiles._num_groups() == 0:
            ifiles._group("all")
        #
        return ifiles.groups

    def verify_dynamic_targets(self, target):
        yield None
        return True

    def process_depends_args(self, dfiles: sos_targets, **kwargs):
        for k in kwargs.keys():
            if k not in SOS_DEPENDS_OPTIONS:
                raise RuntimeError(f"Unrecognized depends option {k}")
        if dfiles.undetermined():
            raise ValueError(r"Depends needs to handle undetermined")

        if env.sos_dict.get("__dynamic_depends__", False):
            runner = self.verify_dynamic_targets(
                [x for x in dfiles if isinstance(x, file_target)]
            )
            try:
                yreq = next(runner)
                while True:
                    yres = yield yreq
                    yreq = runner.send(yres)
            except StopIteration:
                pass

        env.sos_dict.set("_depends", dfiles)
        env.sos_dict.set("step_depends", dfiles)

    def process_output_args(self, ofiles: sos_targets, **kwargs):
        for k in kwargs.keys():
            if k not in SOS_OUTPUT_OPTIONS:
                raise RuntimeError(f"Unrecognized output option {k}")

        if ofiles._num_groups() > 0:
            if ofiles._num_groups() == 1:
                ofiles = ofiles._get_group(0)
            elif ofiles._num_groups() != len(self._substeps):
                raise RuntimeError(
                    f"Inconsistent number of output ({ofiles._num_groups()}) and input ({len(self._substeps)}) groups."
                )
            else:
                ofiles = ofiles._get_group(env.sos_dict["_index"])

        # create directory
        if ofiles.valid():
            parents = set(
                [
                    os.path.abspath(os.path.join(ofile, os.pardir))
                    for ofile in ofiles
                    if isinstance(ofile, file_target)
                ]
            )
            for parent_dir in parents:
                if parent_dir and not os.path.isdir(parent_dir):
                    os.makedirs(parent_dir, exist_ok=True)

        # set variables
        env.sos_dict.set("_output", ofiles)
        env.sos_dict.set("step_output", ofiles)
        #
        for ofile in ofiles:
            oname = ofile.target_name()
            if oname in self._all_outputs:
                raise ValueError(
                    f'Output {ofile} from substep {env.sos_dict["_index"]} of {env.sos_dict["__num_groups__"]} substeps overlaps with output from a previous substep.'
                )
            self._all_outputs.add(oname)

    def submit_task(self, task_info):
        if self.task_manager is None:
            if self.step.task_params:
                for key in ("trunk_size", "trunk_workers", "queue"):
                    val = get_value_of_param(
                        key, self.step.task_params, extra_dict=env.sos_dict.dict()
                    )
                    if val:
                        env.sos_dict["_runtime"][key] = val[0]

            if "trunk_size" in env.sos_dict["_runtime"]:
                trunk_size = env.sos_dict["_runtime"]["trunk_size"]
                if trunk_size is None or trunk_size <= 0:
                    trunk_size = env.sos_dict["__num_groups__"]
                if not isinstance(trunk_size, int):
                    raise ValueError(
                        f'An integer value or None is expected for runtime option trunk_size, "{trunk_size}" provided'
                    )
            else:
                trunk_size = 1
            if "trunk_workers" in env.sos_dict["_runtime"]:
                if "nodes" in env.sos_dict["_runtime"]:
                    raise ValueError(
                        'Option "trunk_workers" that specifies number of nodes and processes for the execution '
                        'of single-node jobs and option "nodes" that specifies number of nodes for single multi-node '
                        "jobs cannot be used at the same time."
                    )
                trunk_workers = env.sos_dict["_runtime"]["trunk_workers"]
            else:
                trunk_workers = None

            # if 'queue' in env.sos_dict['_runtime'] and env.sos_dict['_runtime']['queue']:
            #    host = env.sos_dict['_runtime']['queue']
            # else:
            #    # otherwise, use workflow default
            #    host = '__default__'
            self.task_manager = TaskManager(
                env.sos_dict["__num_groups__"], trunk_size, trunk_workers
            )

        task_id = task_info["task_id"]
        task_index = task_info["index"]
        if task_id is None:
            self.task_manager.set(task_index, None)
            return None

        taskdef = task_info["task_def"]
        task_vars = task_info["task_vars"]

        # 618
        # it is possible that identical tasks are executed (with different underlying random numbers)
        # we should either give a warning or produce different ids...
        if self.task_manager.index_of(task_id) >= 0:
            raise RuntimeError(
                f'Task {task_id} generated for (_index={env.sos_dict["_index"]}) is identical to a previous one (_index={self.task_manager.index_of(task_id)}).'
            )
        elif self.task_manager.has_output(task_vars["_output"]):
            raise RuntimeError(
                f'Task produces output files {", ".join(task_vars["_output"])} that are output of other tasks.'
            )
        # if no trunk_size, the job will be submitted immediately
        # otherwise tasks will be accumulated and submitted in batch
        self.task_manager.set(task_index, (task_id, taskdef, task_vars["_output"]))
        tasks = self.task_manager.get_job()
        if tasks:
            self.submit_tasks(tasks)
        return task_id

    def wait_for_results(self, all_submitted):
        # this is a generator function because wait_for_tasks is a generator
        # function and needs to yield to the caller
        if self.concurrent_substep:
            try:
                runner = self.wait_for_substep()
                yreq = next(runner)
                while True:
                    yres = yield yreq
                    yreq = runner.send(yres)
            except StopIteration:
                pass

        if self.task_manager is None:
            return {}

        #
        # report task
        # what we should do here is to get the alias of the Host
        # because it can be different (e.g. not localhost
        queue = env.sos_dict["_runtime"]["queue"]

        # submit the last batch of tasks
        tasks = self.task_manager.get_job(all_tasks=True)
        if tasks:
            self.submit_tasks(tasks)

        # waiting for results of specified IDs
        try:
            # 1218
            runner = self.wait_for_tasks(
                self.task_manager._submitted_tasks, all_submitted
            )
            yreq = next(runner)
            while True:
                yres = yield yreq
                yreq = runner.send(yres)
        except StopIteration as e:
            results = e.value

        for id, result in results.items():
            # turn to string to avoid naming lookup issue
            rep_result = {
                x: (y if isinstance(y, (int, bool, float, str)) else short_repr(y))
                for x, y in result.items()
            }
            rep_result["tags"] = " ".join(self.task_manager.tags(id))
            rep_result["queue"] = queue
            send_message_to_controller(["workflow_sig", "task", id, repr(rep_result)])
        self.task_manager.clear_submitted()

        # if in dryrun mode, we display the output of the dryrun task
        if env.config["run_mode"] == "dryrun":
            tid = list(results.keys())[0]
            tf = TaskFile(tid)
            if tf.has_stdout():
                print(TaskFile(tid).stdout)

        for idx, task in self.proc_results.items():
            # if it is done
            if isinstance(task, dict):
                continue
            if task in results:
                self.proc_results[idx] = results[task]
            else:
                # can be a subtask
                for _, mres in results.items():
                    if "subtasks" in mres and task in mres["subtasks"]:
                        self.proc_results[idx] = mres["subtasks"][task]
                    # elif 'exception' in mres:
                    #    self.proc_results[idx] = mres
        #
        # check if all have results?
        if any(isinstance(x, str) for x in self.proc_results.values()):
            raise RuntimeError(
                f'Failed to get results for tasks {", ".join(x for x in self.proc_results.values() if isinstance(x, str))}'
            )
        #
        for idx, res in self.proc_results.items():
            if "skipped" in res and res["skipped"]:
                self.completed["__task_skipped__"] += 1
                # complete case: task skipped
                send_message_to_controller(
                    ["progress", "substep_completed", env.sos_dict["step_id"]]
                )
            else:
                # complete case: task completed
                send_message_to_controller(
                    ["progress", "substep_ignored", env.sos_dict["step_id"]]
                )
                self.completed["__task_completed__"] += 1
            if "shared" in res:
                self.shared_vars[idx].update(res["shared"])

    def log(self, stage=None, msg=""):
        if stage == "start":
            env.logger.info(
                f'{"Checking" if env.config["run_mode"] == "dryrun" else "Running"} ``{self.step.step_name(True)}``: {self.step.comment.strip()}'
            )
        elif stage == "input statement":
            if "STEP" in env.config["SOS_DEBUG"] or "ALL" in env.config["SOS_DEBUG"]:
                env.log_to_file("STEP", f"Handling input statement {msg}")
        elif stage == "_input":
            if env.sos_dict["_input"] is not None and len(env.sos_dict["_input"]) > 0:
                env.logger.debug(
                    f'_input: ``{short_repr(env.sos_dict["_input"])}``{msg}'
                )
        elif stage == "_depends":
            if env.sos_dict["_depends"] is not None:
                env.logger.debug(
                    f'_depends: ``{short_repr(env.sos_dict["_depends"])}``{msg}'
                )
        elif stage == "input":
            if env.sos_dict["step_input"] is not None:
                env.logger.info(
                    f'input:   ``{short_repr(env.sos_dict["step_input"])}``{msg}'
                )
        elif stage == "output":
            if (
                env.sos_dict["step_output"] is not None
                and len(env.sos_dict["step_output"]) > 0
            ):
                env.logger.info(
                    f'``{self.step.step_name(True)}`` output:   ``{short_repr(env.sos_dict["step_output"])}``{msg}'
                )

    def execute(self, stmt, return_result=False):
        try:
            self.last_res = SoS_exec(
                stmt,
                return_result=return_result or env.config["run_mode"] == "interactive",
            )
            if return_result:
                return self.last_res
        except (StopInputGroup, TerminateExecution, UnavailableLock):
            raise
        except subprocess.CalledProcessError as e:
            raise RuntimeError(e.stderr)
        except ArgumentError:
            raise
        except ProcessKilled:
            raise
        except KeyboardInterrupt as e:
            raise RuntimeError(get_traceback_msg(e))
        except Exception as e:
            raise RuntimeError(get_traceback_msg(e))

    def prepare_substep(self):
        # socket to collect result
        self.result_pull_socket = create_socket(
            env.zmq_context, zmq.PULL, "substep result collector"
        )
        local_ip = get_localhost_ip()
        port = self.result_pull_socket.bind_to_random_port(f"tcp://{local_ip}")
        env.config["sockets"]["result_push_socket"] = f"tcp://{local_ip}:{port}"

    def submit_substep(self, param):
        send_message_to_controller(["substep", param])

    def process_returned_substep_result(self, till=None, wait=True):
        while True:
            if not wait:
                # 1213
                cur_index = env.sos_dict["_index"]
                pending_substeps = cur_index - self._completed_concurrent_substeps + 1
                if pending_substeps < (
                    100
                    if isinstance(self.concurrent_substep, bool)
                    else self.concurrent_substep
                ):
                    if not self.result_pull_socket.poll(0):
                        return
                elif (
                    "STEP" in env.config["SOS_DEBUG"]
                    or "ALL" in env.config["SOS_DEBUG"]
                ):
                    # if there are more than 100 pending substeps
                    # we wait indefinitely for the results
                    env.log_to_file(
                        "STEP",
                        f"Wait for more substeps to be done before submitting. (index={cur_index}, processed={self._completed_concurrent_substeps})",
                    )
            elif self._completed_concurrent_substeps == till:
                return
            yield self.result_pull_socket
            res = decode_msg(self.result_pull_socket.recv())
            if "exception" in res:
                if isinstance(res["exception"], ProcessKilled):
                    raise res["exception"]
                elif isinstance(res["exception"], RemovedTarget):
                    pass
                elif env.config["error_mode"] == "ignore":
                    idx_msg = (
                        f'(id={env.sos_dict["step_id"]}, index={res["index"]})'
                        if "index" in res and len(self._substeps) > 1
                        else f'(id={env.sos_dict["step_id"]})'
                    )
                    env.logger.warning(
                        f"""Ignoring error from ``{self.step.step_name(True)}`` {idx_msg}: {res["exception"]}."""
                    )
                    res["output"] = sos_targets(invalid_target())
                elif env.config["error_mode"] == "abort":
                    idx_msg = (
                        f'(id={env.sos_dict["step_id"]}, index={res["index"]})'
                        if "index" in res and len(self._substeps) > 1
                        else f'(id={env.sos_dict["step_id"]})'
                    )
                    self.exec_error.append(idx_msg, res["exception"])
                    # try to stop everything but wait till for submitted tasks to
                    # complete
                    self._completed_concurrent_substeps + 1
                    waiting = till - 1 - self._completed_concurrent_substeps
                    env.logger.warning(
                        f'``{self.step.step_name(True)}`` {idx_msg} returns an error.{f" Terminating step after completing {waiting} submitted substeps." if waiting else " Terminating now."}'
                    )
                    for i in range(waiting):
                        yield self.result_pull_socket
                        res = decode_msg(self.result_pull_socket.recv())
                        if "exception" in res:
                            self.exec_error.append(
                                f'index={res["index"]}', res["exception"]
                            )
                    raise self.exec_error
                else:
                    # default or unspecified
                    idx_msg = (
                        f'(id={env.sos_dict["step_id"]}, index={res["index"]})'
                        if "index" in res and len(self._substeps) > 1
                        else f'(id={env.sos_dict["step_id"]})'
                    )
                    self.exec_error.append(idx_msg, res["exception"])
            #
            if "index" not in res:
                raise RuntimeError(
                    "Result received from substep does not have key index"
                )
            if "task_id" in res:
                task = self.submit_task(res)
                # if substep returns tasks, ...
                if res["task_id"]:
                    self.proc_results[res["index"]] = task
                else:
                    # if there is no task_id, the substep must have
                    # been skipped.
                    self.proc_results[res["index"]] = res
            else:
                self.proc_results[res["index"]] = res
            self._completed_concurrent_substeps += 1

    def wait_for_substep(self):
        while self._completed_concurrent_substeps < len(self.proc_results):
            try:
                runner = self.process_returned_substep_result(
                    till=len(self.proc_results), wait=True
                )
                yreq = next(runner)
                while True:
                    yres = yield yreq
                    yreq = runner.send(yres)
            except StopIteration:
                pass

    def collect_result(self):
        # only results will be sent back to the master process
        #
        # __step_input__:    input of this step
        # __steo_output__:   output of this step
        # __step_depends__:  dependent files of this step

        result = {
            "__step_input__": env.sos_dict["step_input"],
            "__step_output__": env.sos_dict["step_output"],
            "__step_depends__": env.sos_dict["step_depends"],
            "__step_name__": env.sos_dict["step_name"],
            "__completed__": self.completed,
        }
        result["__last_res__"] = self.last_res
        result["__shared__"] = {}
        if "shared" in self.step.options:
            result["__shared__"] = self.shared_vars
        for x in result["__step_output__"].targets:
            if isinstance(x, sos_variable):
                result["__shared__"][x.target_name()] = env.sos_dict[x.target_name()]
        send_message_to_controller(
            [
                "progress",
                "step_completed",
                -1
                if "sos_run" in env.sos_dict["__signature_vars__"]
                else self.completed["__step_completed__"],
                env.sos_dict["step_name"],
                env.sos_dict["step_output"],
            ]
        )
        return result

    def set_task_queue_from_task_params(self):
        if self.step.task_params:
            try:
                task_queue = get_value_of_param(
                    "queue", self.step.task_params, extra_dict=env.sos_dict.dict()
                )
                if task_queue:
                    env.sos_dict["_runtime"]["queue"] = task_queue[0]
            except Exception as e:
                raise ValueError(
                    f"Failed to determine value of parameter queue of {self.step.task_params}: {e}"
                )
            # # check concurrent #1134
            # try:
            #     task_concurrency = get_value_of_param(
            #         'concurrent',
            #         self.step.task_params,
            #         extra_dict=env.sos_dict.dict())
            #     if task_concurrency:
            #         env.sos_dict['_runtime']['concurrent'] = task_concurrency[0]
            # except Exception as e:
            #     raise ValueError(
            #         f'Failed to determine value of parameter queue of {self.step.task_params}: {e}'
            #     )
        # if -q is unspecified and option queue is unspecified,
        # or queue=None is specified, disregard the task keyword
        if (
            env.config["default_queue"] is None
            and "queue" not in env.sos_dict["_runtime"]
        ) or (
            "queue" in env.sos_dict["_runtime"]
            and env.sos_dict["_runtime"]["queue"] is None
        ):
            # remove task statement
            if len(self.step.statements) >= 1 and self.step.statements[-1][0] == "!":
                self.step.statements[-1][1] += "\n" + self.step.task
            else:
                self.step.statements.append(["!", self.step.task])
            self.step.task = None
        # is queue is unspecified, it take value from command line
        # in this case -q should have been specified
        elif "queue" not in env.sos_dict["_runtime"]:
            env.sos_dict["_runtime"]["queue"] = env.config["default_queue"]

    def local_exec_without_signature(self, statement):
        idx = env.sos_dict["_index"]
        env.log_to_file(
            "STEP", f'Execute substep {env.sos_dict["step_name"]} without signature'
        )
        try:
            if self.is_input_verified:
                verify_input()
                self.is_input_verified = False
            if env.sos_dict.get("__concurrent_subworkflow__", False):
                self._subworkflow_results.append(
                    self.execute(statement[1], return_result=True)
                )
            else:
                self.execute(statement[1])
            if not self.step.task and env.config["run_mode"] != "interactive":
                env.logger.info(
                    f'``{self.step.step_name(True)}``{f" (index={idx})" if len(self._substeps) > 1 else ""} is ``completed``{" (pending nested workflow)" if self._subworkflow_results else ""}.'
                )
        finally:
            if not self.step.task:
                # if no task, this step is __completed
                # complete case: local skip without task
                send_message_to_controller(
                    ["progress", "substep_completed", env.sos_dict["step_id"]]
                )
        if "shared" in self.step.options:
            try:
                self.shared_vars[env.sos_dict["_index"]].update(
                    {
                        x: env.sos_dict[x]
                        for x in self.vars_to_be_shared
                        if x in env.sos_dict
                    }
                )
            except Exception as e:
                raise ValueError(f"Missing shared variable {e}.")

    def local_exec_with_signature(self, statement, sig):
        idx = env.sos_dict["_index"]
        # signature might be built outside of the function
        # not in a debug mode delayed to now
        if sig is None:
            sig = RuntimeInfo(
                statementMD5([statement[1], self.step.task]),
                env.sos_dict["_input"],
                env.sos_dict["_output"],
                env.sos_dict["_depends"],
                env.sos_dict["__signature_vars__"],
                shared_vars=self.vars_to_be_shared,
            )
            # if singaure match, we skip the substep even  if
            # there are tasks.
            matched = validate_step_sig(sig)
            if matched:
                if env.sos_dict["step_output"].undetermined():
                    self.output_groups[idx] = matched["output"]
                if "vars" in matched:
                    self.shared_vars[idx].update(matched["vars"])
                return True

        env.log_to_file(
            "STEP",
            f'Execute substep {env.sos_dict["step_name"]} with signature {sig.sig_id}',
        )
        sig.lock()
        try:
            if self.is_input_verified:
                verify_input()
                self.is_input_verified = False
            if env.sos_dict.get("__concurrent_subworkflow__", False):
                self._subworkflow_results.append(
                    self.execute(statement[1], return_result=True)
                )
            else:
                self.execute(statement[1])
            if not self.step.task and env.config["run_mode"] != "interactive":
                env.logger.info(
                    f'``{self.step.step_name(True)}``{f" (index={idx})" if len(self._substeps) > 1 else ""} is ``completed``{" (pending nested workflow)" if self._subworkflow_results else ""}.'
                )
            if "shared" in self.step.options:
                try:
                    self.shared_vars[env.sos_dict["_index"]].update(
                        {
                            x: env.sos_dict[x]
                            for x in self.vars_to_be_shared
                            if x in env.sos_dict
                        }
                    )
                except Exception as e:
                    raise ValueError(f"Missing shared variable {e}.")
        finally:
            # if this is the end of substep, save the signature
            # otherwise we need to wait for the completion
            # of the task.
            if not self.step.task:
                if env.sos_dict["step_output"].undetermined():
                    output = reevaluate_output()
                    self.output_groups[env.sos_dict["_index"]] = output
                    sig.set_output(output)
                sig.write()
                # complete case : local execution without task
                send_message_to_controller(
                    ["progress", "substep_completed", env.sos_dict["step_id"]]
                )
            else:
                self.pending_signatures[idx] = sig
            sig.release()
        return False

    def skip_substep(self):
        idx = env.sos_dict["_index"]
        # if concurrent substep, there might be later steps that needs to be rerun
        # and we need to mark some steps has been completed.
        if self.concurrent_substep:
            self._completed_concurrent_substeps += 1
            self.proc_results[idx] = {
                "index": idx,
                "ret_code": 0,
                "output": copy.deepcopy(env.sos_dict["_output"]),
            }
        send_message_to_controller(
            ["progress", "substep_ignored", env.sos_dict["step_id"]]
        )

    def concurrent_exec(self, statement, sig=None):
        idx = env.sos_dict["_index"]
        env.log_to_file(
            "STEP",
            f'Execute substep {env.sos_dict["step_name"]} {idx} concurrently with {self._completed_concurrent_substeps} completed',
        )

        # the ignatures are supposed to be written by substep worker, however
        # the substep worker might send tasks back to the step worker and
        # we should write the signatures after the tasks are completed
        if (
            env.config["sig_mode"] != "ignore"
            and not env.sos_dict["_output"].unspecified()
            and self.step.task
        ):
            self.pending_signatures[idx] = (
                sig
                if sig
                else RuntimeInfo(
                    statementMD5([statement[1], self.step.task]),
                    env.sos_dict["_input"],
                    env.sos_dict["_output"],
                    env.sos_dict["_depends"],
                    env.sos_dict["__signature_vars__"],
                    shared_vars=self.vars_to_be_shared,
                )
            )
        #
        # step_output: needed only when it is undetermined
        # step_input: not needed
        # _input, _output, _depends, _index: needed
        # step_name: for debug scripts
        # step_id, workflow_id: for reporting to controller
        # '__signature_vars__' to be used for signature creation
        #
        # __step_context__ is not needed because substep
        # executor does not support nested workflow

        proc_vars = (
            env.sos_dict["__signature_vars__"]
            | env.sos_dict["__environ_vars__"]
            | {
                "_input",
                "_output",
                "_depends",
                "_index",
                "step_output",
                "step_name",
                "_runtime",
                "step_id",
                "workflow_id",
                "__num_groups__",
                "__signature_vars__",
            }
        )
        self.proc_results[env.sos_dict["_index"]] = {}
        self.submit_substep(
            dict(
                stmt=statement[1],
                global_def=self.step.global_def,
                # 1225: the step might contain large variables from global section, but
                # we do not have to sent them if they are not used in substeps.
                global_vars={
                    x: y
                    for x, y in self.step.global_vars.items()
                    if x in env.sos_dict["__signature_vars__"]
                },
                task=self.step.task,
                task_params=self.step.task_params,
                proc_vars=env.sos_dict.clone_selected_vars(proc_vars),
                shared_vars=self.vars_to_be_shared,
                config=env.config,
            )
        )

    def check_task_sig(self):
        idx = env.sos_dict["_index"]
        sig = RuntimeInfo(
            statementMD5([self.step.task]),
            env.sos_dict["_input"],
            env.sos_dict["_output"],
            env.sos_dict["_depends"],
            env.sos_dict["__signature_vars__"],
            shared_vars=self.vars_to_be_shared,
        )
        env.log_to_file(
            "STEP",
            f'Check task-only step {env.sos_dict["step_name"]} with signature {sig.sig_id}',
        )
        matched = validate_step_sig(sig)
        skip_index = bool(matched)
        if matched:
            if env.sos_dict["step_output"].undetermined():
                self.output_groups[env.sos_dict["_index"]] = matched["output"]
            self.shared_vars[env.sos_dict["_index"]].update(matched["vars"])
            # complete case: step with task ignored
            send_message_to_controller(
                ["progress", "substep_ignored", env.sos_dict["step_id"]]
            )
        self.pending_signatures[idx] = sig
        return skip_index

    # def is_task_active(self):
    #     active = env.sos_dict['_runtime']['active']
    #     env.logger.error(active)
    #     if active is True:
    #         return True
    #     elif active is False:
    #         return False
    #     elif isinstance(active, int):
    #         if active >= 0 and env.sos_dict['_index'] != active:
    #             return False
    #         if active < 0 and env.sos_dict[
    #                 '_index'] != active + env.sos_dict['__num_groups__']:
    #             return False
    #         return True
    #     elif isinstance(active, Sequence):
    #         allowed_index = list([
    #             x if x >= 0 else env.sos_dict['__num_groups__'] + x
    #             for x in active
    #         ])
    #         return env.sos_dict['_index'] in allowed_index
    #     elif isinstance(active, slice):
    #         allowed_index = list(range(env.sos_dict['__num_groups__']))[active]
    #         return env.sos_dict['_index'] in allowed_index
    #     else:
    #         raise RuntimeError(
    #             f'Unacceptable value for option active: {active}')

    def check_results(self):
        for proc_result in [
            x for x in self.proc_results.values() if x["ret_code"] == 0
        ]:
            if "stdout" in proc_result and proc_result["stdout"]:
                sys.stdout.write(proc_result["stdout"])
            if "stderr" in proc_result and proc_result["stderr"]:
                sys.stderr.write(proc_result["stderr"])

        # now that output is settled, we can write remaining signatures
        for idx, res in self.proc_results.items():
            if (
                self.pending_signatures[idx] is not None
                and res["ret_code"] == 0
                and "sig_skipped" not in res
            ):
                # task might return output with vars #1355
                self.pending_signatures[idx].set_output(self.output_groups[idx])
                self.pending_signatures[idx].write()
            if res["ret_code"] != 0 and "output" in res:
                clear_output(output=res["output"])

        for proc_result in [
            x for x in self.proc_results.values() if x["ret_code"] != 0
        ]:
            if "stdout" in proc_result and proc_result["stdout"]:
                sys.stdout.write(proc_result["stdout"])
            if "stderr" in proc_result and proc_result["stderr"]:
                sys.stderr.write(proc_result["stderr"])
            if "exception" in proc_result:
                excp = proc_result["exception"]
                if isinstance(excp, StopInputGroup):
                    if excp.message:
                        env.logger.info(excp.message)
                    self.output_groups[proc_result["index"]] = sos_targets([])
                elif isinstance(excp, RemovedTarget):
                    raise excp
                elif "task" in proc_result:
                    if env.config["error_mode"] == "ignore":
                        env.logger.warning(f"Ignore failed task {proc_result['task']}.")
                    # if the exception is from a task...
                    self.exec_error.append(proc_result["task"], excp)
            else:
                self.exec_error.append(
                    RuntimeError(
                        f"Substep failed with return code {proc_result['ret_code']}"
                    )
                )

        # this is after all substeps have been completed
        if self.exec_error.errors:
            raise self.exec_error

    def calculate_completed(self):
        substeps = (
            self.completed["__substep_completed__"]
            + self.completed["__substep_skipped__"]
        )
        self.completed["__step_completed__"] = (
            self.completed["__substep_completed__"] / substeps
        )
        self.completed["__step_skipped__"] = (
            self.completed["__substep_skipped__"] / substeps
        )
        if self.completed["__step_completed__"].is_integer():
            self.completed["__step_completed__"] = int(
                self.completed["__step_completed__"]
            )
        if self.completed["__step_skipped__"].is_integer():
            self.completed["__step_skipped__"] = int(self.completed["__step_skipped__"])

    def run(self):
        """Execute a single step and return results. The result for batch mode is the
        input, output etc returned as alias, and for interactive mode is the return value
        of the last expression."""
        # return value of the last executed statement
        self.last_res = None
        self.start_time = time.time()
        self.completed = defaultdict(int)
        #
        # prepare environments, namely variables that can be used by the step
        #
        # * step_name:  name of the step, can be used by step process to determine
        #               actions dynamically.
        env.sos_dict.set("step_name", self.step.step_name())
        env.sos_dict.set("__last_step__", self.step.last_step)
        self.log("start")
        env.sos_dict.set(
            "step_id",
            textMD5(
                f'{env.sos_dict["workflow_id"]} {env.sos_dict["step_name"]} {self.step.md5}'
            ),
        )
        env.sos_dict.set("master_id", env.config["master_id"])
        # used by nested workflow
        env.sos_dict.set("__step_context__", self.step.context)

        env.sos_dict.set("_runtime", {})
        # * input:      input files, which should be __step_output__ if it is defined, or
        #               None otherwise.
        # * _input:     first batch of input, which should be input if no input statement is used
        # * output:     None at first, can be redefined by output statement
        # * _output:    None at first, can be redefined by output statement
        # * depends:    None at first, can be redefined by depends statement
        # * _depends:   None at first, can be redefined by depends statement
        #
        self.init_input_output_vars()

        # _index is needed for pre-input action's active option and for debug output of scripts
        env.sos_dict.set("_index", 0)

        if "STEP" in env.config["SOS_DEBUG"] or "ALL" in env.config["SOS_DEBUG"]:
            env.log_to_file(
                "STEP",
                f'Executing step {env.sos_dict["step_name"]} with step_input {env.sos_dict["step_input"]} and step_output {env.sos_dict["step_output"]}',
            )

        self.set_task_queue_from_task_params()

        # look for input statement.
        input_statement_idx = [
            idx
            for idx, x in enumerate(self.step.statements)
            if x[0] == ":" and x[1] == "input"
        ]
        if not input_statement_idx:
            input_statement_idx = None
        elif len(input_statement_idx) == 1:
            input_statement_idx = input_statement_idx[0]
        else:
            raise ValueError(
                f"More than one step input are specified in step {self.step.step_name(True)}"
            )

        # if shared is true, we have to disable concurrent because we
        # do not yet return anything from shared.
        self.concurrent_substep = "shared" not in self.step.options
        # and \
        #     ('concurrent' not in env.sos_dict['_runtime'] or env.sos_dict['_runtime']['concurrent'] is True)
        if input_statement_idx is not None:
            # execute before input stuff
            for statement in self.step.statements[:input_statement_idx]:
                if statement[0] == ":":
                    # wait for all dependent targets to be resolved to be resolved
                    key, value = statement[1:3]
                    if key != "depends":
                        raise ValueError(f"Step input should be specified before {key}")
                    while True:
                        try:
                            args, kwargs = SoS_eval(
                                f"__null_func__({value})",
                                extra_dict={
                                    "__null_func__": __null_func__,
                                    "output_from": __output_from__,
                                    "named_output": __named_output__,
                                    "traced": __traced__,
                                },
                            )
                            dfiles = expand_depends_files(*args)
                            # dfiles can be Undetermined
                            runner = self.process_depends_args(dfiles, **kwargs)
                            try:
                                yreq = next(runner)
                                while True:
                                    yres = yield yreq
                                    yreq = runner.send(yres)
                            except StopIteration:
                                pass
                        except (UnknownTarget, RemovedTarget) as e:
                            runner = self.handle_unknown_target(e)
                            try:
                                yreq = next(runner)
                                while True:
                                    yres = yield yreq
                                    yreq = runner.send(yres)
                            except StopIteration:
                                pass
                            continue
                        except UnavailableLock:
                            raise
                        except Exception as e:
                            raise RuntimeError(
                                f"Failed to process step {key} ({value.strip()}): {e}"
                            )
                        break
                else:
                    try:
                        # 1354
                        # if there are definition before input, the definitions and imports
                        # must be added to global_def in order to be executed by substeps
                        if any(x in statement[1] for x in ("class", "def", "import")):
                            step_def = KeepOnlyImportAndDefine().visit(
                                ast.parse(statement[1])
                            )
                            if step_def.body:
                                if isinstance(self.step.global_def, ast.Module):
                                    self.step.global_def.body.extend(step_def.body)
                                else:
                                    self.step.global_def = step_def
                        self.execute(statement[1])
                    except StopInputGroup as e:
                        # stop before substeps, because there is no output statement before it
                        # we do not have to worry about keep_output
                        if e.message:
                            env.logger.info(e.message)
                        return self.collect_result()
            # input statement
            stmt = self.step.statements[input_statement_idx][2]
            self.log("input statement", stmt)
            while True:
                # wait for all targets to be resovled
                try:
                    args, kwargs = SoS_eval(
                        f"__null_func__({stmt})",
                        extra_dict={
                            "__null_func__": __null_func__,
                            "output_from": __output_from__,
                            "named_output": __named_output__,
                            "traced": __traced__,
                        },
                    )
                    # Files will be expanded differently with different running modes
                    input_files: sos_targets = expand_input_files(
                        *args,
                        **{
                            k: v
                            for k, v in kwargs.items()
                            if k not in SOS_INPUT_OPTIONS
                        },
                    )
                    runner = self.process_input_args(
                        input_files,
                        **{k: v for k, v in kwargs.items() if k in SOS_INPUT_OPTIONS},
                    )
                    try:
                        yreq = next(runner)
                        while True:
                            yres = yield yreq
                            yreq = runner.send(yres)
                    except StopIteration as e:
                        self._substeps = e.value
                    #
                    if "concurrent" in kwargs and self.concurrent_substep:
                        # concurrent can be True/False or an integer
                        self.concurrent_substep = kwargs["concurrent"]
                except (UnknownTarget, RemovedTarget) as e:
                    runner = self.handle_unknown_target(e)
                    try:
                        yreq = next(runner)
                        while True:
                            yres = yield yreq
                            yreq = runner.send(yres)
                    except StopIteration:
                        pass
                    continue
                except UnavailableLock:
                    raise
                except Exception as e:
                    raise ValueError(f"Failed to process input statement {stmt}: {e}")
                break

            input_statement_idx += 1
        elif env.sos_dict["step_input"].groups:
            # if default has groups...
            # default case
            self._substeps = env.sos_dict["step_input"].groups
            # assuming everything starts from 0 is after input
            input_statement_idx = 0
        else:
            # default case
            self._substeps = [env.sos_dict["step_input"]]
            # assuming everything starts from 0 is after input
            input_statement_idx = 0

        self.proc_results = {}
        self.vars_to_be_shared = set()
        if "shared" in self.step.options:
            self.vars_to_be_shared = parse_shared_vars(self.step.options["shared"])
        self.vars_to_be_shared = sorted(
            [
                x[5:] if x.startswith("step_") else x
                for x in self.vars_to_be_shared
                if x not in ("step_", "step_input", "step_output", "step_depends")
            ]
        )
        self.shared_vars = [{} for x in self._substeps]
        # run steps after input statement, which will be run multiple times for each input
        # group.
        env.sos_dict.set("__num_groups__", len(self._substeps))

        # determine if a single index or the whole step should be skipped
        skip_index = False
        # signatures of each index, which can remain to be None if no output
        # is defined.
        self.output_groups = [sos_targets([]) for x in self._substeps]
        self.depends_groups = [sos_targets([]) for x in self._substeps]

        # used to prevent overlapping output from substeps
        self._all_outputs = set()
        self._subworkflow_results = []

        if (
            any("sos_run" in x[1] for x in self.step.statements[input_statement_idx:])
            and "shared" not in self.step.options
            and not self.step.task
            and self.step.statements[-1][0] == "!"
            and (len(self.step.statements) == 1 or self.step.statements[-2][0] == ":")
            and is_sos_run_the_only_last_stmt(self.step.statements[-1][1])
        ):
            env.sos_dict.set("__concurrent_subworkflow__", True)

        if self.concurrent_substep:
            if len(self._substeps) <= 1 or env.config["run_mode"] == "dryrun":
                self.concurrent_substep = False
            elif any(
                "sos_run" in x[1] for x in self.step.statements[input_statement_idx:]
            ):
                self.concurrent_substep = False
                env.logger.debug(
                    "Substeps are executed sequentially because of existence of multiple nested workflow."
                )
            else:
                self.prepare_substep()

        try:
            self.completed["__substep_skipped__"] = 0
            self.completed["__substep_completed__"] = len(self._substeps)
            self._completed_concurrent_substeps = 0
            # pending signatures are signatures for steps with external tasks
            self.pending_signatures = [None for x in self._substeps]

            for idx, g in enumerate(self._substeps):
                #
                # https://github.com/vatlab/sos/issues/1376
                #
                #   [default]
                #   input: for_each=dict(i=range(1000))
                #   sos_run('a', t=i)
                #
                # when we have workflow like the following when steps
                # are executed quickly with subworkflows submitted to the master
                # the master process could be swamped with subworkflows, causing
                # "too many open files".
                #
                # the following code will stop the step from continued
                # execution and wait for the subworkflows to complete.
                #
                if self._subworkflow_results:
                    try:
                        runner = self.wait_for_subworkflows(
                            allow_pending=env.config["worker_procs"]
                        )
                        yreq = next(runner)
                        while True:
                            yres = yield yreq
                            yreq = runner.send(yres)
                    except StopIteration:
                        pass

                # other variables
                #
                _vars = {}
                # now, let us expose target level variables as lists
                if len(g) > 1:
                    names = set.union(*[set(x._dict.keys()) for x in g._targets])
                elif len(g) == 1:
                    names = set(g._targets[0]._dict.keys())
                else:
                    names = set()
                for name in names:
                    _vars[name] = [x.get(name) for x in g._targets]
                # then we expose all group level variables
                _vars.update(g._dict)
                _vars.update(env.sos_dict["step_input"]._dict)
                env.sos_dict.update(_vars)

                env.sos_dict.set("_input", copy.deepcopy(g))
                # set vars to _input
                # env.sos_dict['_input'].set(**v)

                self.log("_input")
                env.sos_dict.set("_index", idx)

                if env.config["error_mode"] == "ignore":
                    missed = [x for x in g.targets if not x.target_exists()]
                    if missed:
                        if any(isinstance(x, invalid_target) for x in missed):
                            env.logger.warning(
                                f'{self.step.step_name(True)}{f" (index={idx})" if len(self._substeps) > 1 else ""} ignored due to invalid input caused by previous failed substep.'
                            )
                        else:
                            env.logger.warning(
                                f'{self.step.step_name(True)}{f" (index={idx})" if len(self._substeps) > 1 else ""} ignored due to missing input {sos_targets(missed)}'
                            )
                        self.output_groups[idx] = sos_targets(invalid_target())
                        env.sos_dict.set("_output", sos_targets(invalid_target()))
                        self.skip_substep()
                        continue

                # in interactive mode, because sos_dict are always shared
                # execution of a substep, especially when it calls a nested
                # workflow, would change step_name, __step_context__ etc, and
                # we will have to reset these variables to make sure the next
                # substep would execute normally. Batch mode is immune to this
                # problem because nested workflows are executed in their own
                # process/context etc
                if env.config["run_mode"] == "interactive":
                    env.sos_dict.set("step_name", self.step.step_name())
                    env.sos_dict.set(
                        "step_id",
                        hash(
                            (
                                env.sos_dict["workflow_id"],
                                env.sos_dict["step_name"],
                                self.step.md5,
                            )
                        ),
                    )
                    # used by nested workflow
                    env.sos_dict.set("__step_context__", self.step.context)
                #
                pre_statement = []
                if (
                    not any(
                        st[0] == ":" and st[1] == "output"
                        for st in self.step.statements[input_statement_idx:]
                    )
                    and "__default_output__" in env.sos_dict
                ):
                    pre_statement = [[":", "output", "_output"]]

                # if there is no statement, no task, claim success
                post_statement = []
                if not self.step.statements or self.step.statements[-1][0] != "!":
                    if self.step.task:
                        # if there is only task, we insert a fake statement so that it can be executed by the executor
                        post_statement = [["!", ""]]
                    else:
                        # complete case: no step, no statement
                        send_message_to_controller(
                            ["progress", "substep_completed", env.sos_dict["step_id"]]
                        )

                all_statements = (
                    pre_statement
                    + self.step.statements[input_statement_idx:]
                    + post_statement
                )
                self.is_input_verified = True
                for statement_idx, statement in enumerate(all_statements):
                    is_last_runblock = statement_idx == len(all_statements) - 1

                    # if input is undertermined, we can only process output:
                    if not g.valid() and statement[0] != ":":
                        raise RuntimeError("Undetermined input encountered")
                    if statement[0] == ":":
                        key, value = statement[1:3]
                        # output, depends, and process can be processed multiple times
                        while True:
                            # loop for all unresolved targets to be resolved
                            try:
                                args, kwargs = SoS_eval(
                                    f"__null_func__({value})",
                                    extra_dict={
                                        "__null_func__": __null_func__,
                                        "output_from": __output_from__,
                                        "named_output": __named_output__,
                                        "traced": __traced__,
                                    },
                                )
                                # dynamic output or dependent files
                                if key == "output":
                                    # if output is defined, its default value needs to be cleared
                                    if idx == 0:
                                        env.sos_dict.set("step_output", sos_targets())
                                    ofiles: sos_targets = expand_output_files(
                                        value,
                                        *args,
                                        **{
                                            k: v
                                            for k, v in kwargs.items()
                                            if k not in SOS_OUTPUT_OPTIONS
                                        },
                                    )
                                    if g.valid() and ofiles.valid():
                                        if any(
                                            x in g._targets
                                            for x in ofiles
                                            if not isinstance(x, sos_step)
                                        ):
                                            raise RuntimeError(
                                                f'Overlapping input and output files: {", ".join(repr(x) for x in ofiles if x in g)}'
                                            )

                                    # set variable _output and output
                                    self.process_output_args(
                                        ofiles,
                                        **{
                                            k: v
                                            for k, v in kwargs.items()
                                            if k in SOS_OUTPUT_OPTIONS
                                        },
                                    )
                                    self.output_groups[idx] = env.sos_dict["_output"]
                                elif key == "depends":
                                    try:
                                        dfiles = expand_depends_files(*args)
                                        # dfiles can be Undetermined
                                        runner = self.process_depends_args(
                                            dfiles, **kwargs
                                        )
                                        try:
                                            yreq = next(runner)
                                            while True:
                                                yres = yield yreq
                                                yreq = runner.send(yres)
                                        except StopIteration:
                                            pass
                                        self.depends_groups[idx] = env.sos_dict[
                                            "_depends"
                                        ]
                                        self.log("_depends")
                                    except Exception:
                                        # env.logger.info(e)
                                        raise
                                else:
                                    raise RuntimeError(f"Unrecognized directive {key}")
                                # everything is ok, break
                                break
                            except (UnknownTarget, RemovedTarget) as e:
                                runner = self.handle_unknown_target(e)
                                try:
                                    yreq = next(runner)
                                    while True:
                                        yres = yield yreq
                                        yreq = runner.send(yres)
                                except StopIteration:
                                    pass
                                continue
                            except UnavailableLock:
                                raise
                            except Exception as e:
                                # if input is Undertermined, it is possible that output cannot be processed
                                # due to that, and we just return
                                if not g.valid():
                                    env.logger.debug(e)
                                    return self.collect_result()
                                raise RuntimeError(
                                    f"Failed to process step {key} ({value.strip()}): {e}"
                                )
                    elif is_last_runblock:
                        if (
                            env.config["sig_mode"] == "skip"
                            and not self.vars_to_be_shared
                            and "sos_run" not in statement[1]
                            and not env.sos_dict["_output"].unspecified()
                            and len(env.sos_dict["_output"]) > 0
                            and all(
                                x.target_exists()
                                for x in env.sos_dict["_output"].targets
                            )
                            and env.sos_dict["_output"].later_than(
                                env.sos_dict["_input"]
                            )
                        ):
                            self.skip_substep()
                            env.logger.info(
                                f'``{env.sos_dict["step_name"]}``{f" (index={idx})" if len(self._substeps) > 1 else ""} is ``skipped`` with existing output.'
                            )
                            skip_index = True
                            # do not execute the rest of the statement
                            break
                        #
                        # default mode, check if skipping substep
                        sig = None
                        if (
                            env.config["sig_mode"]
                            not in ("ignore", "distributed", "build")
                            and not env.sos_dict["_output"].unspecified()
                        ):
                            sig = RuntimeInfo(
                                statementMD5([statement[1], self.step.task]),
                                env.sos_dict["_input"],
                                env.sos_dict["_output"],
                                env.sos_dict["_depends"],
                                env.sos_dict["__signature_vars__"],
                                shared_vars=self.vars_to_be_shared,
                            )
                            matched = validate_step_sig(sig)
                            skip_index = bool(matched)
                            if skip_index:
                                # matched["output"] might hav vars not defined in "output" #1355
                                env.sos_dict.set("_output", matched["output"])
                                self.output_groups[idx] = matched["output"]
                                if "vars" in matched:
                                    self.shared_vars[idx].update(matched["vars"])
                                self.skip_substep()

                                break
                        try:
                            if self.concurrent_substep:
                                self.concurrent_exec(statement, sig)
                                # we check if the previous task has been completed and process them
                                # because further steps might need to be done
                                try:
                                    runner = self.process_returned_substep_result(
                                        till=idx + 1, wait=False
                                    )
                                    yreq = next(runner)
                                    while True:
                                        yres = yield yreq
                                        yreq = runner.send(yres)
                                except StopIteration:
                                    pass
                            elif (
                                env.config["sig_mode"] == "ignore"
                                or env.sos_dict["_output"].unspecified()
                            ):
                                self.local_exec_without_signature(statement)
                            else:
                                skip_index = self.local_exec_with_signature(
                                    statement, sig
                                )
                                if skip_index:
                                    self.skip_substep()
                                    break

                        except StopInputGroup as e:
                            if not e.keep_output:
                                clear_output()
                                self.output_groups[idx] = sos_targets([])
                            if e.message:
                                env.logger.info(e.message)
                            skip_index = True
                            break
                        except Exception as e:
                            clear_output()
                            if env.config["error_mode"] == "abort":
                                raise
                            elif env.config["error_mode"] == "ignore":
                                idx_msg = (
                                    f'(id={env.sos_dict["step_id"]}, index={idx})'
                                    if len(self._substeps) > 1
                                    else f'(id={env.sos_dict["step_id"]})'
                                )
                                env.logger.warning(
                                    f"{self.step.step_name(True)} {idx_msg} returns no output due to error: {e}"
                                )
                                self.output_groups[idx] = sos_targets(invalid_target())
                                skip_index = True
                            else:
                                if env.config["run_mode"] != "interactive":
                                    # default mode
                                    idx_msg = (
                                        f'(id={env.sos_dict["step_id"]}, index={idx})'
                                        if len(self._substeps) > 1
                                        else f'(id={env.sos_dict["step_id"]})'
                                    )
                                    env.logger.error(
                                        f"{self.step.step_name(True)} {idx_msg} returns an error."
                                    )
                                self.exec_error.append(str(idx), e)
                    else:
                        # if it is not the last statement group (e.g. statements before :output)
                        # we execute locally without anything like signature
                        if self.is_input_verified:
                            verify_input()
                            self.is_input_verified = False
                        try:
                            self.execute(statement[1])
                        except StopInputGroup as e:
                            if not e.keep_output:
                                clear_output()
                                self.output_groups[idx] = sos_targets([])
                            if e.message:
                                env.logger.info(e.message)
                            skip_index = True
                            break
                        except Exception:
                            clear_output()
                            raise
                # if there is no statement , but there are tasks, we should
                # check signature here.
                if (
                    (not self.step.statements or self.step.statements[-1][0] != "!")
                    and self.step.task
                    and not self.concurrent_substep
                    and env.config["sig_mode"] != "ignore"
                    and not env.sos_dict["_output"].unspecified()
                ):
                    skip_index = self.check_task_sig()

                # if this index is skipped, go directly to the next one
                if skip_index:
                    self.completed["__substep_skipped__"] += 1
                    self.completed["__substep_completed__"] -= 1
                    skip_index = False
                    continue

                # if concurrent input group, tasks are handled in substep
                if self.concurrent_substep or not self.step.task:
                    continue

                if env.config["run_mode"] == "dryrun" and env.sos_dict["_index"] != 0:
                    continue

                # # check if the task is active
                # if 'active' in env.sos_dict['_runtime']:
                #     if not self.is_task_active():
                #         continue
                #
                self.log("task")
                try:
                    task_id, taskdef, task_vars = create_task(
                        self.step.global_def,
                        self.step.global_vars,
                        self.step.task,
                        self.step.task_params,
                    )
                    task = self.submit_task(
                        {
                            "index": env.sos_dict["_index"],
                            "task_id": task_id,
                            "task_def": taskdef,
                            "task_vars": task_vars,
                        }
                    )
                    self.proc_results[env.sos_dict["_index"]] = task
                except Exception as e:
                    # FIXME: cannot catch exception from subprocesses
                    if env.verbosity > 2:
                        sys.stderr.write(get_traceback())
                    raise RuntimeError(
                        f'Failed to execute process\n"{short_repr(self.step.task)}"\n{e}'
                    )

                #
                # # if not concurrent, we have to wait for the completion of the task
                # if 'concurrent' in env.sos_dict['_runtime'] and env.sos_dict[
                #         '_runtime']['concurrent'] is False:
                #     # in this case the steps must be executed not concurrently
                #     runner = self.wait_for_results(all_submitted=False)
                #     try:
                #         yreq = next(runner)
                #         while True:
                #             yres = yield yreq
                #             yreq = runner.send(yres)
                #     except StopIteration:
                #         pass
                #
                # endfor loop for each input group
                #
            if self._subworkflow_results:
                try:
                    runner = self.wait_for_subworkflows(allow_pending=0)
                    yreq = next(runner)
                    while True:
                        yres = yield yreq
                        yreq = runner.send(yres)
                except StopIteration:
                    pass
                env.sos_dict.pop("__concurrent_subworkflow__")

            runner = self.wait_for_results(all_submitted=True)
            try:
                yreq = next(runner)
                while True:
                    yres = yield yreq
                    yreq = runner.send(yres)
            except StopIteration:
                pass

            for idx, res in self.proc_results.items():
                if "sig_skipped" in res:
                    self.completed["__substep_skipped__"] += 1
                    self.completed["__substep_completed__"] -= 1
                if "output" in res:
                    self.output_groups[idx] = res["output"]

            # check results
            self.check_results()

            # if error happened but we allow all substeps to be completed, we now
            # raise exception
            if self.exec_error.errors:
                raise self.exec_error

            # if output is Undetermined, re-evalulate it
            # finalize output from output_groups because some output might be skipped
            # this is the final version of the output but we do maintain output
            # during the execution of step, for compatibility.
            env.sos_dict.set(
                "step_output", sos_targets([])._add_groups(self.output_groups)
            )
            env.sos_dict.set(
                "step_depends", sos_targets([])._add_groups(self.depends_groups)
            )

            # if there exists an option shared, the variable would be treated as
            # provides=sos_variable(), and then as step_output
            if "shared" in self.step.options:
                self.shared_vars = evaluate_shared(
                    self.shared_vars, self.step.options["shared"]
                )
                env.sos_dict.quick_update(self.shared_vars)
            missing = self.verify_output()
            self.log(
                "output",
                msg=f'\033[95m missing: {short_repr(missing)} ({len(missing)} item{"s" if len(missing)>1 else ""})\033[0m'
                if len(missing) > 0
                else "",
            )
            self.calculate_completed()

            def file_only(targets):
                if not isinstance(targets, sos_targets):
                    env.logger.warning(
                        f"Unexpected input or output target for reporting. Empty list returned: {targets}"
                    )
                    return []
                return [
                    (str(x), x.size())
                    for x in targets._targets
                    if isinstance(x, file_target)
                ]

            step_info = {
                "step_id": self.step.md5,
                "start_time": self.start_time,
                "stepname": self.step.step_name(True),
                "substeps": len(self._substeps),
                "input": file_only(env.sos_dict["step_input"]),
                "output": file_only(env.sos_dict["step_output"]),
                "completed": dict(self.completed),
                "end_time": time.time(),
            }
            send_message_to_controller(
                ["workflow_sig", "step", env.sos_dict["workflow_id"], repr(step_info)]
            )
            return self.collect_result()
        finally:
            if self.concurrent_substep:
                close_socket(self.result_pull_socket, "substep collector")


class Step_Executor(Base_Step_Executor):
    """Single process step executor"""

    def __init__(self, step, socket, mode="run"):
        self.run_mode = mode
        env.config["run_mode"] = mode
        super(Step_Executor, self).__init__(step)
        self.socket = socket
        # because step is executed in a separate SoS_Worker process, this
        # __socket__ is available to all the actions that will be executed
        # in the step
        env.__socket__ = socket

    def submit_tasks(self, tasks):
        if "TASK" in env.config["SOS_DEBUG"] or "ALL" in env.config["SOS_DEBUG"]:
            env.log_to_file("TASK", f"Send {tasks}")
        self.socket.send(
            encode_msg(["tasks", env.sos_dict["_runtime"]["queue"]] + tasks)
        )

    def wait_for_tasks(self, tasks, all_submitted):
        # wait for task is a generator function that yields the request
        # to the runner
        if not tasks:
            return {}
        # when we wait, the "outsiders" also need to see the tags etc
        # of the tasks so we have to write to the database. #156
        send_message_to_controller(["commit_sig"])

        # wait till the executor responde
        results = {}
        while True:
            # yield an indicator of what is requested, for debugging purpose
            yield self.socket
            res = decode_msg(self.socket.recv())
            if res is None:
                sys.exit(0)
            results.update(res)

            # all results have been obtained.
            if len(results) == len(tasks):
                break
        return results

    def wait_for_subworkflows(self, allow_pending):
        """Wait for results from subworkflows"""
        try:
            allow_pending = int(allow_pending)
        except:
            allow_pending = min(max(os.cpu_count() // 2, 2), 8)

        while self._subworkflow_results:
            if allow_pending > 0:
                n_pending = sum(
                    len(x["pending_workflows"]) for x in self._subworkflow_results
                )
                if n_pending <= allow_pending:
                    break
            # here we did not check if workflow ids match
            yield self.socket
            res = decode_msg(self.socket.recv())
            if res is None:
                sys.exit(0)
            elif isinstance(res, Exception):
                raise res
            if not "__workflow_id__" in res:
                raise ValueError(f"Unrecognized result from subworkflows: {res}")
            # remove from _self._subworkflow_results
            result_with_id = [
                idx
                for idx, x in enumerate(self._subworkflow_results)
                if res["__workflow_id__"] in x["pending_workflows"]
            ]
            if not result_with_id:
                raise RuntimeError(
                    f"Failed to identify ID of returned subworkflow: {res}"
                )
            if len(result_with_id) > 1:
                raise RuntimeError(
                    "Multiple matches of subworkflow ID. This should not happen."
                )
            self._subworkflow_results[result_with_id[0]]["pending_workflows"].remove(
                res["__workflow_id__"]
            )
            if not self._subworkflow_results[result_with_id[0]]["pending_workflows"]:
                self._subworkflow_results.pop(result_with_id[0])

    def handle_unknown_target(self, e):
        self.socket.send(encode_msg(["missing_target", e.target]))
        yield self.socket
        res = decode_msg(self.socket.recv())
        if not res:
            raise e

    def verify_dynamic_targets(self, targets):
        if not targets:
            return

        if env.config["trace_existing"]:
            traced = targets
        else:
            traced = [x for x in targets if x.traced]

        if not traced:
            return

        self.socket.send(encode_msg(["dependent_target"] + traced))
        yield self.socket
        res = decode_msg(self.socket.recv())
        if res != "target_resolved":
            raise RuntimeError(f"Failed to veryify dependent target {traced}")

    def run(self):
        try:
            try:
                # 1218
                runner = Base_Step_Executor.run(self)
                yreq = next(runner)
                while True:
                    yres = yield yreq
                    yreq = runner.send(yres)
            except StopIteration as e:
                res = e.value

            if self.socket is not None:
                if (
                    "STEP" in env.config["SOS_DEBUG"]
                    or "ALL" in env.config["SOS_DEBUG"]
                ):
                    env.log_to_file(
                        "STEP",
                        f"Step {self.step.step_name()} sends result {short_repr(res)}",
                    )
                self.socket.send(encode_msg(res))
            else:
                return res
        except RemovedTarget as e:
            # removed target needs to be handled differently since the workflow manager
            # use type information to get removed targets
            if self.socket is not None and not self.socket.closed:
                self.socket.send(encode_msg(e))
            else:
                raise e
        except Exception as e:
            if env.verbosity > 2:
                sys.stderr.write(get_traceback())
            if isinstance(e, ProcessKilled):
                raise
            # if not self.exec_error
            if e != self.exec_error:
                self.exec_error.append(self.step.step_name(), e)
        #
        if self.exec_error.errors:
            if self.socket is not None and not self.socket.closed:
                env.log_to_file(
                    "STEP",
                    f"Step {self.step.step_name()} sends exception {self.exec_error}",
                )
                self.socket.send(encode_msg(self.exec_error))
            else:
                raise self.exec_error
