#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.
import copy
import os
import fasteners
import pickle
import time
import lzma
import math
import struct
from enum import Enum
from collections import namedtuple
from collections.abc import Sequence
from datetime import datetime

from typing import Union, Dict, List

from .utils import (
    env,
    expand_time,
    linecount_of_file,
    sample_lines,
    short_repr,
    tail_of_file,
    pretty_size,
    expand_size,
    format_HHMMSS,
    DelayedAction,
    format_duration,
)
from .targets import sos_targets

monitor_interval = 5
resource_monitor_interval = 60


class TaskParams(object):
    """A parameter object that encaptulates parameters sending to
    task executors. This would makes the output of workers, especially
    in the web interface much cleaner (issue #259)"""

    def __init__(self, name, global_def, task, sos_dict, tags):
        self.name = name
        self.global_def = global_def
        self.task = task
        self.sos_dict = sos_dict
        self.tags = tags
        # remove builtins that could be saved in a dictionary
        if "CONFIG" in self.sos_dict and "__builtins__" in self.sos_dict["CONFIG"]:
            self.sos_dict["CONFIG"].pop("__builtins__")

    def __repr__(self):
        return self.name


class MasterTaskParams(TaskParams):
    def __init__(self, num_workers=None):
        self.ID = "t0"
        self.name = self.ID
        self.global_def = ""
        self.task = ""

        self.sos_dict = {
            "_runtime": {"num_workers": num_workers},
            "_input": sos_targets(),
            "_output": sos_targets(),
            "_depends": sos_targets(),
            "step_input": sos_targets(),
            "step_output": sos_targets(),
            "step_depends": sos_targets(),
            "step_name": "",
            "_index": 0,
        }
        self.tags = []
        # a collection of tasks that will be executed by the master task
        self.task_stack = []

    def _parse_num_workers(self, num_workers):
        # return number of nodes and workers

        if isinstance(num_workers, Sequence) and len(num_workers) >= 1:
            val = str(num_workers[0])
            n_workers = val.rsplit(":", 1)[-1] if ":" in val else val
            n_nodes = len(num_workers)
        elif isinstance(num_workers, str):
            n_workers = (
                num_workers.rsplit(":", 1)[-1] if ":" in num_workers else num_workers
            )
            n_nodes = 1
        elif isinstance(num_workers, int):
            n_workers = num_workers
            n_nodes = 1
        elif num_workers is None:
            n_workers = 1
            n_nodes = 1
        else:
            raise RuntimeError(
                f"Unacceptable value for parameter trunk_workers {num_workers}"
            )

        try:
            n_workers = int(n_workers)
        except Exception:
            raise ValueError(
                f"Unacceptable value for option trunk_workers {num_workers}"
            )
        if n_workers <= 0:
            raise ValueError(
                f"Unacceptable value for option trunk_workers {num_workers}"
            )

        return n_nodes, n_workers

    def num_tasks(self):
        return len(self.task_stack)

    def push(self, task_id, params):
        # update walltime, cores, and mem
        # right now we require all tasks to have same resource requirment, which is
        # quite natural because they are from the same step
        #
        # update input, output, and depends
        #
        # walltime etc
        n_nodes, n_workers = self._parse_num_workers(
            self.sos_dict["_runtime"]["num_workers"]
        )

        if not self.task_stack:
            for key in (
                "walltime",
                "max_walltime",
                "cores",
                "nodes",
                "max_cores",
                "mem",
                "max_mem",
                "name",
                "workdir",
                "verbosity",
                "sig_mode",
                "run_mode",
            ):
                if (
                    key in params.sos_dict["_runtime"]
                    and params.sos_dict["_runtime"][key] is not None
                ):
                    self.sos_dict["_runtime"][key] = params.sos_dict["_runtime"][key]
            self.sos_dict["step_name"] = params.sos_dict["step_name"]
            self.tags = params.tags
        else:
            for key in (
                "walltime",
                "max_walltime",
                "cores",
                "max_cores",
                "mem",
                "max_mem",
                "name",
                "workdir",
            ):
                val0 = self.task_stack[0][1].sos_dict["_runtime"].get(key, None)
                val = params.sos_dict["_runtime"].get(key, None)
                if val0 != val:
                    raise ValueError(f"All tasks should have the same resource {key}")

                if val0 is None:
                    continue

                # If there are multiple nodes and multiple workers, there are
                # n_workers * n_nodes workers at the same time, so the jobs
                # will be completed in n_batches
                n_batches = math.ceil(
                    (len(self.task_stack) + 1) / (n_workers * n_nodes)
                )

                if key == "walltime":
                    # the real walltime would be the total time on one node
                    self.sos_dict["_runtime"]["walltime"] = format_HHMMSS(
                        n_batches * expand_time(val0)
                    )
                elif key == "mem":
                    # number of columns * mem for each + 100M for master
                    self.sos_dict["_runtime"]["mem"] = n_workers * expand_size(val0)
                elif key == "cores":
                    self.sos_dict["_runtime"]["cores"] = n_workers * val0
                elif key == "name":
                    self.sos_dict["_runtime"][
                        "name"
                    ] = f"{val0}_{len(self.task_stack) + 1}"

            self.tags.extend(params.tags)

        # if cores is unspecified but there are more than one workers
        if (
            "cores" not in self.sos_dict["_runtime"]
            and n_workers is not None
            and n_workers > 1
        ):
            self.sos_dict["_runtime"]["cores"] = n_workers
        #
        # input, output, preserved vars etc
        for key in ["_input", "_output", "_depends"]:
            if key in params.sos_dict and isinstance(params.sos_dict[key], sos_targets):
                if key == "__builtins__":
                    continue
                # do not extend duplicated input etc
                self.sos_dict[key].extend(params.sos_dict[key])
        #
        self.task_stack.append([task_id, params])
        self.tags = sorted(list(set(self.tags)))
        #
        id_prefix = f't{len(self.task_stack)}'
        self.ID = f"{id_prefix}{self.task_stack[0][0][:-(len(id_prefix))]}"
        self.name = self.ID

    def finalize(self):
        if not self.task_stack:
            return
        common_dict = None
        common_keys = set()
        for _, params in self.task_stack:
            if common_dict is None:
                common_dict = params.sos_dict
                common_keys = set(params.sos_dict.keys())
            else:
                common_keys = {
                    key
                    for key in common_keys
                    if key in params.sos_dict
                    and common_dict[key] == params.sos_dict[key]
                }
            if not common_keys:
                break
        # if there is only one subtask, _output will be moved out of subtasks and makes
        # the retrival of outputs difficult.
        common_keys.discard("_output")
        self.common_dict = {x: common_dict[x] for x in common_keys}
        for _, params in self.task_stack:
            params.sos_dict = {
                k: v for k, v in params.sos_dict.items() if k not in common_keys
            }
        #
        n_nodes = self._parse_num_workers(self.sos_dict["_runtime"]["num_workers"])[0]
        # trunk_workers and cores cannot be specified together, so if n_nodes > 1,
        # nodes should not have been specified.
        if n_nodes is not None and n_nodes > 1:
            self.sos_dict["_runtime"]["nodes"] = n_nodes

        return self


def combine_results(task_id, results):
    # now we collect result
    all_res = {
        "ret_code": 0,
        "output": None,
        "subtasks": {},
        "shared": {},
        "skipped": 0,
        "signature": {},
    }
    for res in results:
        tid = res["task"]
        all_res["subtasks"][tid] = res

        if "exception" in res:
            all_res["exception"] = res["exception"]
            all_res["ret_code"] += 1
            continue
        all_res["ret_code"] += res["ret_code"]
        if all_res["output"] is None:
            all_res["output"] = copy.deepcopy(res["output"])
        else:
            try:
                all_res["output"].extend(res["output"], keep_groups=True)
            except Exception:
                env.logger.warning(
                    f"Failed to extend output {all_res['output']} with {res['output']}"
                )
        all_res["shared"].update(res["shared"])
        # does not care if one or all subtasks are executed or skipped.
        all_res["skipped"] += res.get("skipped", 0)
        if "signature" in res:
            all_res["signature"].update(res["signature"])

    if all_res["ret_code"] != 0:
        if all_res["ret_code"] == len(results):
            if env.config["run_mode"] == "run":
                env.logger.info(f"All {len(results)} tasks in {task_id} ``failed``")
            else:
                env.logger.debug(f"All {len(results)} tasks in {task_id} ``failed``")
        else:
            if env.config["run_mode"] == "run":
                env.logger.info(
                    f'{all_res["ret_code"]} of {len(results)} tasks in {task_id} ``failed``'
                )
            else:
                env.logger.debug(
                    f'{all_res["ret_code"]} of {len(results)} tasks in {task_id} ``failed``'
                )

        # if some failed, some skipped, not skipped
        if "skipped" in all_res:
            all_res.pop("skipped")
    elif all_res["skipped"]:
        if all_res["skipped"] == len(results):
            if env.config["run_mode"] == "run":
                env.logger.info(
                    f"All {len(results)} tasks in {task_id} ``ignored`` or skipped"
                )
            else:
                env.logger.debug(
                    f"All {len(results)} tasks in {task_id} ``ignored`` or skipped"
                )
        else:
            # if only partial skip, we still save signature and result etc
            if env.config["run_mode"] == "run":
                env.logger.info(
                    f'{all_res["skipped"]} of {len(results)} tasks in {task_id} ``ignored`` or skipped'
                )
            else:
                env.logger.debug(
                    f'{all_res["skipped"]} of {len(results)} tasks in {task_id} ``ignored`` or skipped'
                )

            all_res.pop("skipped")
    else:
        if env.config["run_mode"] == "run":
            env.logger.info(f"All {len(results)} tasks in {task_id} ``completed``")
        else:
            env.logger.debug(f"All {len(results)} tasks in {task_id} ``completed``")
    return all_res


class TaskStatus(Enum):
    new = 0
    pending = 1
    submitted = 2
    running = 3
    aborted = 4
    failed = 5
    completed = 6


class TaskFile(object):
    """
    The task file has the following format:

    1. A binary header with the information of the structure of the file
    with field defined by TaskHeader
    2. compressed pickled param of task
    3. compressed pulse file
    4. compressed pickled result
    5. compressed stdout
    6. compressed stderr
    7. compressed pickled signatures
    """

    TaskHeader_v1 = namedtuple(
        "TaskHeader",
        "version status last_modified "
        "new_time pending_time submitted_time running_time aborted_time failed_time completed_time "
        "params_size pulse_size stdout_size stderr_size result_size signature_size "
        "tags",
    )

    TaskHeader_v2 = namedtuple(
        "TaskHeader",
        "version status last_modified "
        "new_time pending_time submitted_time running_time aborted_time failed_time completed_time "
        "params_size shell_size pulse_size stdout_size stderr_size result_size signature_size "
        "tags",
    )

    TaskHeader_v3 = namedtuple(
        "TaskHeader",
        "version status last_modified "
        "new_time pending_time submitted_time running_time aborted_time failed_time completed_time "
        "params_size runtime_size shell_size pulse_size stdout_size stderr_size result_size signature_size "
        "tags",
    )

    TaskHeader = TaskHeader_v3

    header_fmt_v1 = "!2h 8d 6i 128s"
    header_fmt_v2 = "!2h 8d 7i 124s"
    header_fmt_v3 = "!2h 8d 8i 120s"

    header_fmt = header_fmt_v3

    header_size = 220  # struct.calcsize(header_fmt)
    tags_offset = [92, 96, 100]  # struct.calcsize(status_fmt + '6i')
    tags_size = [128, 124, 120]

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.task_file = os.path.join(
            os.path.expanduser("~"), ".sos", "tasks", task_id + ".task"
        )

    def save(self, params):
        if os.path.isfile(self.task_file):
            if self.status == "running":
                env.logger.debug(f"Task {self.task_id} is running and is not updated")
                return

            # keep original stuff but update params, which could contain
            # new runtime info
            self.params = params
            return
        # updating job_file will not change timestamp because it will be Only
        # the update of runtime info
        now = time.time()
        # we keep in both places because params.tags is the only place to have it for subtasks
        tags = params.tags
        params_block = lzma.compress(pickle.dumps(params))
        # env.logger.error(f'saving {self.task_id} params of size {len(params_block)}')
        header = self.TaskHeader(
            version=3,
            status=TaskStatus.new.value,
            last_modified=now,
            new_time=now,
            pending_time=0,
            running_time=0,
            submitted_time=0,
            aborted_time=0,
            failed_time=0,
            completed_time=0,
            params_size=len(params_block),
            runtime_size=0,
            shell_size=0,
            pulse_size=0,
            stdout_size=0,
            stderr_size=0,
            result_size=0,
            signature_size=0,
            tags=" ".join(sorted(tags)).ljust(128).encode(),
        )
        with fasteners.InterProcessLock(
            os.path.join(env.temp_dir, self.task_id + ".lck")
        ):
            with open(self.task_file, "wb+") as fh:
                self._write_header(fh, header)
                fh.write(params_block)

    def exists(self):
        return os.path.isfile(self.task_file)

    def _reset(self, fh):
        # remove result, input, output etc and set the status of the task to new
        header = self._read_header(fh)
        now = time.time()
        header = header._replace(
            version=2,
            status=TaskStatus.new.value,
            last_modified=now,
            new_time=now,
            pending_time=0,
            submitted_time=0,
            running_time=0,
            aborted_time=0,
            failed_time=0,
            completed_time=0,
            runtime_size=0,
            shell_size=0,
            pulse_size=0,
            stdout_size=0,
            stderr_size=0,
            result_size=0,
            signature_size=0,
        )
        self._write_header(fh, header)
        fh.truncate(self.header_size + header.params_size)
        return header

    def reset(self):
        # remove result, input, output etc and set the status of the task to new
        with fasteners.InterProcessLock(
            os.path.join(env.temp_dir, self.task_id + ".lck")
        ):
            with open(self.task_file, "r+b") as fh:
                self._reset(fh)

    def _read_header(self, fh):
        fh.seek(0, 0)
        data = fh.read(self.header_size)
        if struct.unpack("!h", data[:2])[0] == 1:
            header = self.TaskHeader_v1._make(struct.unpack(self.header_fmt_v1, data))
            if header.version not in (1, 2, 3):
                raise RuntimeError(
                    f"Corrupted task file {self.task_file}. Please report a bug if you can reproduce the generation of this file."
                )
            return self.TaskHeader(
                runtime_size=0, shell_size=0, **header._asdict()
            )._replace(version=3)
        if struct.unpack("!h", data[:2])[0] == 2:
            header = self.TaskHeader_v2._make(struct.unpack(self.header_fmt_v2, data))
            if header.version not in (1, 2, 3):
                raise RuntimeError(
                    f"Corrupted task file {self.task_file}. Please report a bug if you can reproduce the generation of this file."
                )
            return self.TaskHeader(runtime_size=0, **header._asdict())._replace(
                version=3
            )
        header = self.TaskHeader._make(struct.unpack(self.header_fmt, data))
        if header.version not in (1, 2, 3):
            raise RuntimeError(
                f"Corrupted task file {self.task_file}. Please report a bug if you can reproduce the generation of this file."
            )
        return header

    def _write_header(self, fh, header):
        fh.seek(0, 0)
        fh.write(struct.pack(self.header_fmt, *header))

    def _get_content(self, exts):
        if isinstance(exts, str):
            exts = [exts]
        content = b""
        for ext in exts:
            filename = self.task_file[:-5] + ext
            if not os.path.isfile(filename):
                continue
            with open(filename, "rb") as fh:
                content += fh.read()
        if not content:
            return b""
        return lzma.compress(content)

    def add_outputs(self, keep_result=False):
        # get header
        shell = self._get_content(".sh")
        pulse = self._get_content(".pulse")
        stdout = self._get_content([".out", ".sosout"])
        stderr = self._get_content([".err", ".soserr"])
        with fasteners.InterProcessLock(
            os.path.join(env.temp_dir, self.task_id + ".lck")
        ):
            with open(self.task_file, "r+b") as fh:
                header = self._read_header(fh)
                if header.result_size != 0:
                    if not keep_result:
                        result_size = 0
                        signature_size = 0
                    else:
                        result_size = header.result_size
                        signature_size = header.signature_size
                        fh.seek(
                            self.header_size
                            + header.params_size
                            + header.runtime_size
                            + header.shell_size
                            + header.pulse_size
                            + header.stdout_size
                            + header.stderr_size,
                            0,
                        )
                        result = fh.read(header.result_size)
                        signature = fh.read(header.signature_size)
                else:
                    result_size = 0
                    signature_size = 0
                header = header._replace(
                    shell_size=len(shell),
                    pulse_size=len(pulse),
                    stdout_size=len(stdout),
                    stderr_size=len(stderr),
                    result_size=result_size,
                    signature_size=signature_size,
                )
                self._write_header(fh, header)
                fh.seek(self.header_size + header.params_size + header.runtime_size, 0)
                if shell:
                    fh.write(shell)
                if pulse:
                    fh.write(pulse)
                if stdout:
                    fh.write(stdout)
                if stderr:
                    fh.write(stderr)
                if result_size > 0:
                    fh.write(result)
                if signature_size > 0:
                    fh.write(signature)

    def add_result(self, result: dict = {}):
        if not result:
            params = self._get_params()
            # this is a master task, get all sub task IDs
            if hasattr(params, "task_stack"):
                missing_tasks = set([x[0] for x in params.task_stack])
                #
                cache_file = os.path.join(
                    os.path.expanduser("~"), ".sos", "tasks", self.task_id + ".cache"
                )
                results = []
                if os.path.isfile(cache_file):
                    try:
                        with open(cache_file, "rb") as f:
                            while True:
                                res = pickle.load(f)
                                if not "task" in res:
                                    # something is wrong
                                    break
                                missing_tasks.remove(res["task"])
                                results.append(res)
                        os.remove(cache_file)
                    except Exception:
                        # we read until an error occurs
                        pass
                if not results:
                    # if there is no result at all, do not save result
                    return
                else:
                    # now, if we have some results, we need to fill the rest of the aborted ones
                    results.extend(
                        [
                            {
                                "task": t,
                                "ret_code": 2,
                                "shared": {},
                                "exception": RuntimeError(f"Subtask {t} is aborted"),
                            }
                            for t in missing_tasks
                        ]
                    )
                result = combine_results(self.task_id, results)
            else:
                # single task, no result, do not save
                return
        # add signature if exists
        signature = result.get("signature", {})
        result.pop("signature", None)
        #
        result_block = lzma.compress(pickle.dumps(result))
        signature_block = lzma.compress(pickle.dumps(signature)) if signature else b""
        with fasteners.InterProcessLock(
            os.path.join(env.temp_dir, self.task_id + ".lck")
        ):
            with open(self.task_file, "r+b") as fh:
                header = self._read_header(fh)
                header = header._replace(
                    result_size=len(result_block),
                    signature_size=len(signature_block),
                )
                self._write_header(fh, header)
                fh.seek(
                    self.header_size
                    + header.params_size
                    + header.runtime_size
                    + header.shell_size
                    + header.pulse_size
                    + header.stdout_size
                    + header.stderr_size
                )
                fh.write(result_block)
                if signature:
                    fh.write(signature_block)

    def _get_info(self):
        with open(self.task_file, "rb") as fh:
            return self._read_header(fh)

    def _set_info(self, info):
        with open(self.task_file, "r+b") as fh:
            fh.write(struct.pack(self.header_fmt, *info))

    info = property(_get_info, _set_info)

    def has_shell(self):
        return self.info.shell_size > 0

    def has_pulse(self):
        return self.info.pulse_size > 0

    def has_result(self):
        return self.info.result_size > 0

    def has_stdout(self):
        return self.info.stdout_size > 0

    def has_stderr(self):
        return self.info.stderr_size > 0

    def has_signature(self):
        return self.info.signature_size > 0

    def _get_params(self):
        with open(self.task_file, "rb") as fh:
            header = self._read_header(fh)
            if header.params_size == 0 and header.runtime_size == 0:
                return {}
            fh.seek(self.header_size, 0)
            if header.params_size == 0:
                return {}
            else:
                try:
                    return pickle.loads(lzma.decompress(fh.read(header.params_size)))
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to obtain params of task {self.task_id}: {e}"
                    )

    def _set_params(self, params):
        params_block = lzma.compress(pickle.dumps(params))
        # env.logger.error(f'updating {self.task_id} params of size {len(params_block)}')
        with fasteners.InterProcessLock(
            os.path.join(env.temp_dir, self.task_id + ".lck")
        ):
            with open(self.task_file, "r+b") as fh:
                header = self._read_header(fh)
                if len(params_block) == header.params_size:
                    fh.seek(self.header_size, 0)
                    fh.write(params_block)
                else:
                    fh.read(header.params_size)
                    runtime = fh.read(header.runtime_size)
                    shell = fh.read(header.shell_size)
                    pulse = fh.read(header.pulse_size)
                    stdout = fh.read(header.stdout_size)
                    stderr = fh.read(header.stderr_size)
                    result = fh.read(header.result_size)
                    signature = fh.read(header.signature_size)
                    header = header._replace(params_size=len(params_block))
                    self._write_header(fh, header)
                    fh.write(params_block)
                    if runtime:
                        fh.write(runtime)
                    if shell:
                        fh.write(shell)
                    if pulse:
                        fh.write(pulse)
                    if stdout:
                        fh.write(stdout)
                    if stderr:
                        fh.write(stderr)
                    if result:
                        fh.write(result)
                    if signature:
                        fh.write(signature)
                    fh.truncate(
                        self.header_size
                        + header.params_size
                        + header.runtime_size
                        + header.shell_size
                        + header.pulse_size
                        + header.stdout_size
                        + header.stderr_size
                        + header.result_size
                        + header.signature_size
                    )

    params = property(_get_params, _set_params)

    def _get_runtime(self):
        with open(self.task_file, "rb") as fh:
            header = self._read_header(fh)
            if header.runtime_size == 0:
                return {}
            fh.seek(self.header_size + header.params_size, 0)
            try:
                return pickle.loads(lzma.decompress(fh.read(header.runtime_size)))
            except Exception as e:
                env.logger.error(
                    f"Failed to obtain runtime of task {self.task_id}: {e}"
                )
                return {"_runtime": {}}

    def _set_runtime(self, runtime):
        runtime_block = lzma.compress(pickle.dumps(runtime))
        # env.logger.error(f'updating {self.task_id} params of size {len(params_block)}')
        with fasteners.InterProcessLock(
            os.path.join(env.temp_dir, self.task_id + ".lck")
        ):
            with open(self.task_file, "r+b") as fh:
                header = self._read_header(fh)
                if len(runtime_block) == header.runtime_size:
                    fh.seek(self.header_size + header.params_size, 0)
                    fh.write(runtime_block)
                else:
                    params = fh.read(header.params_size)
                    fh.seek(
                        self.header_size + header.params_size + header.runtime_size, 0
                    )
                    shell = fh.read(header.shell_size) if header.shell_size else b""
                    pulse = fh.read(header.pulse_size) if header.pulse_size else b""
                    stdout = fh.read(header.stdout_size) if header.stdout_size else b""
                    stderr = fh.read(header.stderr_size) if header.stderr_size else b""
                    result = fh.read(header.result_size) if header.result_size else b""
                    signature = (
                        fh.read(header.signature_size) if header.signature_size else b""
                    )
                    header = header._replace(runtime_size=len(runtime_block))
                    self._write_header(fh, header)
                    fh.write(params)
                    fh.write(runtime_block)
                    if shell:
                        fh.write(shell)
                    if pulse:
                        fh.write(pulse)
                    if stdout:
                        fh.write(stdout)
                    if stderr:
                        fh.write(stderr)
                    if result:
                        fh.write(result)
                    if signature:
                        fh.write(signature)
                    fh.truncate(
                        self.header_size
                        + header.params_size
                        + header.runtime_size
                        + header.shell_size
                        + header.pulse_size
                        + header.stdout_size
                        + header.stderr_size
                        + header.result_size
                        + header.signature_size
                    )

    runtime = property(_get_runtime, _set_runtime)

    def get_params_and_runtime(self):
        with open(self.task_file, "rb") as fh:
            header = self._read_header(fh)
            if header.params_size == 0 and header.runtime_size == 0:
                return {}
            fh.seek(self.header_size, 0)
            if header.params_size == 0:
                params = {}
            else:
                try:
                    params = pickle.loads(lzma.decompress(fh.read(header.params_size)))
                except Exception as e:
                    env.logger.error(
                        f"Failed to obtain params with runtime of task {self.task_id}: {e}"
                    )
                    params = {}
            if "_runtime" not in params.sos_dict:
                params.sos_dict["_runtime"] = {}
            if header.runtime_size > 0:
                try:
                    runtime = pickle.loads(
                        lzma.decompress(fh.read(header.runtime_size))
                    )
                except Exception as e:
                    env.logger.error(
                        f"Failed to obtain runtime of task {self.task_id}: {e}"
                    )
                    runtime = {"_runtime": {}}
            else:
                runtime = {"_runtime": {}}
            return params, runtime

    def _get_status(self):
        if not os.path.isfile(self.task_file):
            return "missing"
        try:
            with open(self.task_file, "rb") as fh:
                fh.seek(2, 0)
                return TaskStatus(struct.unpack("!h", fh.read(2))[0]).name
        except Exception as e:
            env.logger.warning(
                f"Incompatible task file {self.task_file} is removed. This might was most likely generated by a previous version of SoS but please report a bug if you can reproduce this warning message: {e}"
            )
            os.remove(self.task_file)

    def _get_version(self):
        with open(self.task_file, "rb") as fh:
            fh.seek(0, 0)
            return struct.unpack("!h", fh.read(2))[0]

    version = property(_get_version)

    def _get_last_updated(self):
        with open(self.task_file, "rb") as fh:
            fh.seek(4, 0)
            return struct.unpack("!d", fh.read(8))[0]

    last_updated = property(_get_last_updated)

    def _set_status(self, status):
        with fasteners.InterProcessLock(
            os.path.join(env.temp_dir, self.task_id + ".lck")
        ):
            with open(self.task_file, "r+b") as fh:
                fh.seek(2, 0)
                if status == "skipped":
                    # special status, set completed_time = running_time
                    # to make sure duration is zero
                    now = time.time()
                    sts = TaskStatus["completed"].value
                    # update status and last modified
                    fh.write(struct.pack("!hd", sts, now))
                    # also set 'run'
                    fh.seek(3 * 8, 1)
                    fh.write(struct.pack("!d", now))
                    # from the current location, move by status
                    fh.seek(2 * 8, 1)
                    fh.write(struct.pack("!d", now))
                else:
                    if status == "running":
                        # setting to running status ... refresh the pulse file
                        pulse_file = os.path.join(
                            os.path.expanduser("~"),
                            ".sos",
                            "tasks",
                            self.task_id + ".pulse",
                        )
                        with open(pulse_file, "w") as pd:
                            pd.write(f"#task: {self.task_id}\n")
                            pd.write(
                                f'#started at {datetime.now().strftime("%A, %d. %B %Y %I:%M%p")}\n#\n'
                            )
                        # wait for the pulse file to be created before updating task status
                        while True:
                            if os.path.isfile(pulse_file):
                                break
                            else:
                                time.sleep(0.01)
                    # if completed, we make sure that the duration will not
                    # be zero even if the task is completed very rapidly
                    now = time.time() + (0.01 if status == "completed" else 0)
                    sts = TaskStatus[status].value
                    # update status and last modified
                    fh.write(struct.pack("!hd", sts, now))
                    # from the current location, move by status
                    fh.seek(sts * 8, 1)
                    fh.write(struct.pack("!d", now))
            # if restarting the task, make sure all irrelevant files
            # are removed or finishing tasks.
            if status in ("aborted", "completed", "failed", "pending"):
                # terminal status
                remove_task_files(
                    self.task_id,
                    [
                        ".sh",
                        ".job_id",
                        ".sosout",
                        ".soserr",
                        ".out",
                        ".err",
                        ".pulse",
                        ".cache",
                    ],
                )

    status = property(_get_status, _set_status)

    def _get_tags(self):
        try:
            with open(self.task_file, "rb") as fh:
                fh.seek(0, 0)
                ver = struct.unpack("!h", fh.read(2))[0]
                fh.seek(self.tags_offset[ver - 1], 0)
                return fh.read(self.tags_size[ver - 1]).decode().strip()
        except Exception:
            raise RuntimeError(
                f"Corrupted task file {self.task_file}. Please report a bug if you can reproduce the generation of this file."
            )

    def _set_tags(self, tags: list):
        with open(self.task_file, "r+b") as fh:
            fh.seek(0, 0)
            ver = struct.unpack("!h", fh.read(2))[0]
            fh.seek(self.tags_offset[ver - 1], 0)
            fh.write(" ".join(sorted(tags)).ljust(self.tags_size[ver - 1]).encode())

    tags = property(_get_tags, _set_tags)

    def _get_shell(self):
        with open(self.task_file, "rb") as fh:
            header = self._read_header(fh)
            if header.shell_size == 0:
                return ""
            fh.seek(self.header_size + header.params_size + header.runtime_size, 0)
            try:
                return lzma.decompress(fh.read(header.shell_size)).decode()
            except Exception as e:
                env.logger.warning(f"Failed to decode shell: {e}")
                return ""

    shell = property(_get_shell)

    def _get_pulse(self):
        with open(self.task_file, "rb") as fh:
            header = self._read_header(fh)
            if header.pulse_size == 0:
                return ""
            fh.seek(
                self.header_size
                + header.params_size
                + header.runtime_size
                + header.shell_size,
                0,
            )
            try:
                return lzma.decompress(fh.read(header.pulse_size)).decode()
            except Exception as e:
                env.logger.warning(f"Failed to decode pulse: {e}")
                return ""

    pulse = property(_get_pulse)

    def _get_stdout(self):
        with open(self.task_file, "rb") as fh:
            header = self._read_header(fh)
            if header.stdout_size == 0:
                return ""
            fh.seek(
                self.header_size
                + header.params_size
                + header.runtime_size
                + header.pulse_size
                + header.shell_size,
                0,
            )
            try:
                return lzma.decompress(fh.read(header.stdout_size)).decode()
            except Exception as e:
                env.logger.warning(f"Failed to decode stdout: {e}")
                return ""

    stdout = property(_get_stdout)

    def _get_stderr(self):
        with open(self.task_file, "rb") as fh:
            header = self._read_header(fh)
            if header.stderr_size == 0:
                return ""
            fh.seek(
                self.header_size
                + header.params_size
                + header.runtime_size
                + header.shell_size
                + header.pulse_size
                + header.stdout_size,
                0,
            )
            try:
                return lzma.decompress(fh.read(header.stderr_size)).decode()
            except Exception as e:
                env.logger.warning(f"Failed to decode stderr: {e}")
                return ""

    stderr = property(_get_stderr)

    def _get_result(self):
        with open(self.task_file, "rb") as fh:
            header = self._read_header(fh)
            if header.result_size == 0:
                return {}
            fh.seek(
                self.header_size
                + header.params_size
                + header.runtime_size
                + header.shell_size
                + header.pulse_size
                + header.stdout_size
                + header.stderr_size,
                0,
            )
            try:
                return pickle.loads(lzma.decompress(fh.read(header.result_size)))
            except Exception as e:
                env.logger.warning(f"Failed to decode result: {e}")
                return {"ret_code": 1}

    result = property(_get_result)

    def _get_signature(self):
        with open(self.task_file, "rb") as fh:
            header = self._read_header(fh)
            if header.signature_size == 0:
                return {}
            fh.seek(
                self.header_size
                + header.params_size
                + header.runtime_size
                + header.shell_size
                + header.pulse_size
                + header.stdout_size
                + header.stderr_size
                + header.result_size,
                0,
            )
            try:
                return pickle.loads(lzma.decompress(fh.read(header.signature_size)))
            except Exception as e:
                env.logger.warning(f"Failed to decode signature: {e}")
                return {"ret_code": 1}

    signature = property(_get_signature)

    def tags_created_start_and_duration(self, formatted=False):
        try:
            with open(self.task_file, "rb") as fh:
                header = self._read_header(fh)
            try:
                tags = header.tags.decode().strip()
            except Exception:
                raise ValueError(
                    f"{self.task_file} is in a format that is no longer supported."
                )
            ct = header.new_time
            if header.running_time != 0:
                st = header.running_time
                if TaskStatus(header.status) == TaskStatus.running:
                    dr = time.time() - st
                else:
                    dr = header.last_modified - st
            else:
                return (
                    tags,
                    ("Created " + format_duration(time.time() - ct, True) + " ago")
                    if formatted
                    else ct,
                    "",
                    "",
                )
            if not formatted:
                return tags, ct, st, dr
            #
            return (
                tags,
                "Created " + format_duration(time.time() - ct, True) + " ago",
                "Started " + format_duration(time.time() - st) + " ago",
                ("Ran for " + format_duration(int(dr)))
                if dr > 0
                else "Signature checked",
            )
        except Exception:
            # missing tag file or something went wrong
            return "", "", "", ""


def taskDuration(task):
    filename = os.path.join(os.path.expanduser("~"), ".sos", "tasks", f"{task}.task")
    return os.path.getatime(filename) - os.path.getmtime(filename)


def remove_task_files(task: str, exts: list):
    task_dir = os.path.join(os.path.expanduser("~"), ".sos", "tasks")
    for ext in exts:
        filename = os.path.join(task_dir, task + ext)
        if os.path.isfile(filename):
            try:
                os.remove(filename)
            except Exception:
                # if the file cannot be removed now, we use a thread to wait a
                # bit and try to remove it later. The function should not
                # wait for the thread though
                try:
                    DelayedAction(os.remove, filename)
                except Exception:
                    pass


def check_task(task, hint={}) -> Dict[str, Union[str, Dict[str, float]]]:
    # when testing. if the timestamp is 0, the file does not exist originally, it should
    # still does not exist. Otherwise the file should exist and has the same timestamp
    if (
        hint
        and hint["status"] not in ("pending", "running")
        and all(
            (os.path.isfile(f) and os.stat(f).st_mtime == v)
            if v
            else (not os.path.isfile(f))
            for f, v in hint["files"].items()
        )
    ):
        return {}
    # status of the job, please refer to https://github.com/vatlab/SOS/issues/529
    # for details.
    #
    task_file = os.path.join(os.path.expanduser("~"), ".sos", "tasks", task + ".task")
    if not os.path.isfile(task_file):
        return dict(status="missing", files={task_file: 0})

    mtime = os.stat(task_file).st_mtime

    def task_changed():
        return os.stat(task_file).st_mtime != mtime

    tf = TaskFile(task)
    status = tf.status
    if status in ["failed", "completed", "aborted"]:
        # thse are terminal states. We simply return them
        # only change of the task file will trigger recheck of status
        stdout_file = os.path.join(
            os.path.expanduser("~"), ".sos", "tasks", task + ".sosout"
        )
        stderr_file = os.path.join(
            os.path.expanduser("~"), ".sos", "tasks", task + ".soserr"
        )
        # 1242
        if os.path.isfile(stdout_file) or os.path.isfile(stderr_file):
            tf.add_outputs(keep_result=True)
        # 1323
        tf.add_result()
        remove_task_files(task, [".sosout", ".soserr", ".out", ".err"])
        # stdout and stderr files should not exist
        status_files = {
            task_file: os.stat(task_file).st_mtime,
            stdout_file: 0,
            stderr_file: 0,
        }
        return dict(status=status, files=status_files)

    pulse_file = os.path.join(os.path.expanduser("~"), ".sos", "tasks", task + ".pulse")

    # check the existence and validity of .pulse file
    if os.path.isfile(pulse_file):
        try:
            status_files = {
                task_file: os.stat(task_file).st_mtime,
                pulse_file: os.stat(pulse_file).st_mtime,
            }

            # if we have hint, we know the time stamp of last
            # status file.
            if (
                not hint
                or pulse_file not in hint["files"]
                or status_files[pulse_file] != hint["files"][pulse_file]
            ):
                return dict(status="running", files=status_files)

            elapsed = time.time() - status_files[pulse_file]
            if elapsed < 60:
                return dict(status="running", files=status_files)

            syserr_file = os.path.join(
                os.path.expanduser("~"), ".sos", "tasks", task + ".err"
            )

            # if the system does not return any error message, write sos-specific one
            if os.path.isfile(syserr_file) and os.path.getsize(syserr_file) > 0:
                try:
                    with open(syserr_file) as syserr:
                        env.logger.warning("".join(syserr.readlines()[-5:]))
                except Exception as e:
                    env.logger.warning(
                        f"{task} is suspected to be killed but {syserr_file} cannot be read: {e}"
                    )
            else:
                soserr_file = os.path.join(
                    os.path.expanduser("~"), ".sos", "tasks", task + ".soserr"
                )
                with open(soserr_file, "a") as err:
                    err.write(
                        f"Task {task} inactive for more than {int(elapsed)} seconds, might have been killed."
                    )
                env.logger.warning(
                    f"Task {task} inactive for more than {int(elapsed)} seconds, might have been killed."
                )

            tf.add_outputs()
            # 1323
            tf.add_result()

            # assume aborted
            tf.status = "aborted"
            return dict(
                status="aborted",
                files={task_file: os.stat(task_file).st_mtime, pulse_file: 0},
            )
        except Exception:
            # the pulse file could disappear when the job is completed.
            if task_changed():
                return check_task(task)
            raise
    elif status == "running":
        # starting of task will create a pulse file. If the pulse file is gone
        # and the status is still showing as running, something is wrong.
        # if there is no pulse file .
        tf.status = "aborted"
        with open(
            os.path.join(os.path.expanduser("~"), ".sos", "tasks", task + ".soserr"),
            "a",
        ) as err:
            err.write(f"Task {task} considered as aborted due to missing pulse file.")
        env.logger.warning(
            f"Task {task} considered as aborted due to missing pulse file."
        )
        tf.add_outputs()
        # 1323
        tf.add_result()
        return dict(
            status="aborted",
            files={task_file: os.stat(task_file).st_mtime, pulse_file: 0},
        )

    # if there is no pulse file
    job_file = os.path.join(os.path.expanduser("~"), ".sos", "tasks", task + ".sh")

    def has_job():
        job_id_file = os.path.join(
            os.path.expanduser("~"), ".sos", "tasks", task + ".job_id"
        )
        return (
            os.path.isfile(job_file)
            and os.stat(job_file).st_mtime >= os.stat(task_file).st_mtime
            and os.path.isfile(job_id_file)
            and os.stat(job_id_file).st_mtime >= os.stat(job_file).st_mtime
        )

    if has_job():
        try:
            if status != "submitted":
                tf.status = "submitted"
            return dict(
                status="submitted",
                files={
                    task_file: os.stat(task_file).st_mtime,
                    job_file: os.stat(job_file).st_mtime,
                    pulse_file: 0,
                },
            )
        except Exception:
            # the pulse file could disappear when the job is completed.
            if task_changed():
                return check_task(task)
            else:
                raise
    else:
        # status not changed
        try:
            if (
                hint
                and hint["status"] in ("new", "pending")
                and hint["files"][task_file] == os.stat(task_file).st_mtime
            ):
                return {}
            else:
                return dict(
                    status=status,
                    files={task_file: os.stat(task_file).st_mtime, job_file: 0},
                )
        except Exception:
            # the pulse file could disappear when the job is completed.
            if task_changed():
                return check_task(task)
            else:
                raise


def check_tasks(tasks, is_all: bool):
    if not tasks:
        return {}
    cache_file: str = os.path.join(
        os.path.expanduser("~"), ".sos", "tasks", "status_cache.pickle"
    )
    #
    status_cache: Dict = {}
    if os.path.isfile(cache_file):
        try:
            with fasteners.InterProcessLock(cache_file + "_"):
                with open(cache_file, "rb") as cache:
                    status_cache = pickle.load(cache)
        except Exception:
            # if the cache file is corrupted, remove it. #1275
            os.remove(cache_file)
    # at most 20 threads
    from multiprocessing.pool import ThreadPool as Pool

    p = Pool(min(20, len(tasks)))
    # the result can be {} for unchanged, or real results
    raw_status = p.starmap(check_task, [(x, status_cache.get(x, {})) for x in tasks])

    # if check all, we clear the cache and record all existing tasks
    has_changes: bool = any(x for x in raw_status)
    if has_changes:
        if is_all:
            status_cache = {
                k: v if v else status_cache[k] for k, v in zip(tasks, raw_status)
            }
        else:
            status_cache.update({k: v for k, v in zip(tasks, raw_status) if v})
        with fasteners.InterProcessLock(cache_file + "_"):
            with open(cache_file, "wb") as cache:
                pickle.dump(status_cache, cache)
    return status_cache


def print_task_status(
    tasks,
    check_all=False,
    verbosity: int = 1,
    html: bool = False,
    numeric_times=False,
    age=None,
    tags=None,
    status=None,
):
    # # verbose is ignored for now
    # if not check_all and not tasks:
    #     from .signatures import WorkflowSignatures
    #     workflow_signatures = WorkflowSignatures()
    #     tasks = [
    #         x for x in workflow_signatures.tasks() if os.path.isfile(
    #             os.path.join(
    #                 os.path.expanduser('~'), '.sos', 'tasks', x + '.task'))
    #     ]
    import glob

    all_tasks: List = []
    if check_all:
        tasks = glob.glob(
            os.path.join(os.path.expanduser("~"), ".sos", "tasks", "*.task")
        )
        all_tasks = [(os.path.basename(x)[:-5], os.path.getmtime(x)) for x in tasks]
        if not all_tasks:
            return
    else:
        for t in tasks:
            matched_names = glob.glob(
                os.path.join(os.path.expanduser("~"), ".sos", "tasks", f"{t}*.task")
            )
            matched = [
                (os.path.basename(x)[:-5], os.path.getmtime(x)) for x in matched_names
            ]
            if not matched:
                all_tasks.append((t, None))
            else:
                all_tasks.extend(matched)
    if age is not None:
        age = expand_time(age, default_unit="d")
        if age > 0:
            all_tasks = [x for x in all_tasks if time.time() - x[1] >= age]
        else:
            all_tasks = [x for x in all_tasks if time.time() - x[1] <= -age]

    all_tasks = sorted(list(set(all_tasks)), key=lambda x: 0 if x[1] is None else x[1])

    if tags:
        all_tasks = [
            x
            for x in all_tasks
            if TaskFile(x[0]).exists()
            and any(y in tags for y in TaskFile(x[0]).tags.split())
        ]

    if not all_tasks:
        env.logger.debug("No matching tasks are identified.")
        return

    raw_status = check_tasks([x[0] for x in all_tasks], check_all)
    obtained_status = [raw_status[x[0]]["status"] for x in all_tasks]
    #
    # automatically remove non-running tasks that are more than 30 days old
    to_be_removed = [
        t
        for s, (t, d) in zip(obtained_status, all_tasks)
        if d is not None and time.time() - d > 30 * 24 * 60 * 60 and s != "running"
    ]

    if status:
        all_tasks = [x for x, s in zip(all_tasks, obtained_status) if s in status]
        obtained_status = [x for x in obtained_status if x in status]
    #
    from .monitor import summarizeExecution

    if html:
        # HTML output
        from .utils import isPrimitive
        import pprint

        print('<table width="100%" class="resource_table">')

        def row(th=None, td=None):
            if td is None:
                print(f'<tr><th align="right" width="30%">{th}</th><td></td></tr>')
            elif th is None:
                print(f'<tr><td colspan="2" align="left"  width="30%">{td}</td></tr>')
            else:
                print(
                    f'<tr><th align="right"  width="30%">{th}</th><td align="left"><div class="one_liner">{td}</div></td></tr>'
                )

        for s, (t, d) in zip(obtained_status, all_tasks):
            tf = TaskFile(t)
            ts, ct, st, dr = tf.tags_created_start_and_duration(formatted=True)
            row("ID", t)
            row("Status", s)
            row("Created", ct)
            if st:
                row("Started", st)
            if dr:
                row("Duration", dr)

            params = tf.params
            row("Task")
            if hasattr(params, "task_stack"):
                row(
                    td=f'<pre style="text-align:left">{params.task_stack[0][1].task}</pre>'
                )
            else:
                row(td=f'<pre style="text-align:left">{params.task}</pre>')
            row("Tags")
            row(td=f'<pre style="text-align:left">{tf.tags}</pre>')
            if params.global_def:
                row("Global")
                row(td=f'<pre style="text-align:left">{params.global_def}</pre>')
            # row('Environment')
            global_runtime = tf.runtime["_runtime"]
            job_vars = params.sos_dict
            job_vars["_runtime"].update(global_runtime)
            for k in sorted(job_vars.keys()):
                v = job_vars[k]
                if not k.startswith("__") and not k == "CONFIG":
                    if k == "_runtime":
                        for _k, _v in v.items():
                            if isPrimitive(_v) and _v not in (None, "", [], (), {}):
                                row(_k, _v)
                    elif isPrimitive(v) and v not in (None, "", [], (), {}):
                        row(
                            k, f'<pre style="text-align:left">{pprint.pformat(v)}</pre>'
                        )
            pulse_content = ""

            if tf.has_result():
                if s not in ("pending", "submitted", "running"):
                    res = tf.result

                    if "start_time" in res and "end_time" in res:
                        row(
                            "Duration",
                            format_duration(res["end_time"] - res["start_time"]),
                        )
                    if "peak_cpu" in res:
                        row("Peak CPU", f'{res["peak_cpu"]*100} %')
                    if "peak_mem" in res:
                        row("Peak mem", pretty_size(res["peak_mem"]))

                    # this is a placeholder for the frontend to draw figure
                    row(td=f'<div id="res_{t}"></div>')
            elif s == "running":
                pulse_file = os.path.join(
                    os.path.expanduser("~"), ".sos", "tasks", t + ".pulse"
                )
                if os.path.isfile(pulse_file):
                    with open(pulse_file) as pulse:
                        pulse_content = pulse.read()
                        summary = summarizeExecution(t, pulse_content, status=s)
                        if summary:
                            # row('Execution')
                            for line in summary.split("\n"):
                                fields = line.split(None, 1)
                                if fields[0] == "task":
                                    continue
                                row(fields[0], "" if fields[1] is None else fields[1])
                            # this is a placeholder for the frontend to draw figure
                            row(td=f'<div id="res_{t}"></div>')

            if s not in ("pending", "submitted", "running"):
                #
                if tf.has_shell():
                    shell = tf.shell
                    numLines = shell.count("\n")
                    row("shell", f"{numLines} lines")
                    row(td=f'<small><pre style="text-align:left">{shell}</pre></small>')
                if tf.has_stdout():
                    stdout = tf.stdout
                    numLines = stdout.count("\n")
                    row(
                        "stdout",
                        "(empty)"
                        if numLines == 0
                        else f'{numLines} lines{"" if numLines < 200 else " (showing last 200)"}',
                    )
                    if numLines > 200:
                        stdout = "\n".join(stdout.splitlines()[-200:])
                    row(
                        td=f'<small><pre style="text-align:left">{stdout}</pre></small>'
                    )
                if tf.has_stderr():
                    stderr = tf.stderr
                    numLines = stderr.count("\n")
                    row(
                        "stderr",
                        "(empty)"
                        if numLines == 0
                        else f'{numLines} lines{"" if numLines < 200 else " (showing last 200)"}',
                    )
                    if numLines > 200:
                        stderr = "\n".join(stderr.splitlines()[-200:])
                    row(
                        td=f'<small><pre style="text-align:left">{stderr}</pre></small>'
                    )
            elif s == "running":
                files = glob.glob(
                    os.path.join(os.path.expanduser("~"), ".sos", "tasks", t + ".*")
                )
                for f in sorted(
                    [
                        x
                        for x in files
                        if os.path.splitext(x)[-1] not in (".task", ".pulse")
                    ]
                ):
                    numLines = linecount_of_file(f)
                    rhead = os.path.splitext(f)[-1]
                    if rhead == ".sh":
                        rhead = "shell"
                    elif rhead == ".job_id":
                        rhead = "job ID"
                    elif rhead == ".err":
                        rhead = "stderr"
                    elif rhead == ".out":
                        rhead = "stdout"
                    elif rhead == ".soserr":
                        rhead = "sos error"
                    elif rhead == ".sosout":
                        rhead = "sos output"
                    row(
                        rhead,
                        "(empty)"
                        if numLines == 0
                        else f'{numLines} lines{"" if numLines < 200 else " (showing last 200)"}',
                    )
                    try:
                        row(
                            td=f'<small><pre style="text-align:left">{tail_of_file(f, 200, ansi2html=True)}</pre></small>'
                        )
                    except Exception:
                        row(
                            td='<small><pre style="text-align:left">ignored.</pre><small>'
                        )
            print("</table>")
            #
            if not pulse_content:
                return
            # A sample of 400 point should be enough to show the change of resources
            lines = sample_lines(pulse_content, 400).splitlines()
            if len(lines) <= 2:
                return
            # read the pulse file and plot it
            # time   proc_cpu        proc_mem        children        children_cpu    children_mem
            try:
                etime = []
                cpu = []
                mem = []
                for line in lines:
                    if line.startswith("#") or not line.strip():
                        continue
                    fields = line.split()
                    etime.append(float(fields[0]))
                    cpu.append(float(fields[1]) + float(fields[4]))
                    mem.append(float(fields[2]) / 1e6 + float(fields[5]) / 1e6)
                if not etime:
                    return
            except Exception:
                return
            #
            print(
                """
<script>
    function loadFiles(files, fn) {
        if (!files.length) {
            files = [];
        }
        var head = document.head || document.getElementsByTagName('head')[0];

        function loadFile(index) {
            if (files.length > index) {
                if (files[index].endsWith('.css')) {
                    var fileref = document.createElement('link');
                    fileref.setAttribute("rel", "stylesheet");
                    fileref.setAttribute("type", "text/css");
                    fileref.setAttribute("href", files[index]);
                } else {
                    var fileref = document.createElement('script');
                    fileref.setAttribute("type", "text/javascript");
                    fileref.setAttribute("src", files[index]);
                }
                console.log('Load ' + files[index]);
                head.appendChild(fileref);
                index = index + 1;
                // Used to call a callback function
                fileref.onload = function() {
                    loadFile(index);
                }
            } else if (fn) {
                fn();
            }
        }
        loadFile(0);
    }

function plotResourcePlot_"""
                + t
                + """() {
    // get the item
    // parent element is a table cell, needs enlarge
    document.getElementById(
        "res_"""
                + t
                + """").parentElement.setAttribute("height", "300px;");
    $("#res_"""
                + t
                + """").css("height", "300px");
    $("#res_"""
                + t
                + """").css("width", "100%");
    $("#res_"""
                + t
                + """").css("min-height", "300px");

    var cpu = ["""
                + ",".join([f"[{x*1000},{y}]" for x, y in zip(etime, cpu)])
                + """];
    var mem = ["""
                + ",".join([f"[{x*1000},{y}]" for x, y in zip(etime, mem)])
                + """];

    $.plot('#res_"""
                + t
                + """', [{
                data: cpu,
                label: "CPU (%)"
            },
            {
                data: mem,
                label: "mem (M)",
                yaxis: 2
            }
        ], {
            xaxes: [{
                mode: "time"
            }],
            yaxes: [{
                min: 0
            }, {
                position: "right",
                tickFormatter: function(v, axis) {
                    return v.toFixed(1) + 'M';
                }
            }],
            legend: {
                position: "nw"
            }
        });
}

var dt = 100;
// the frontend might be notified before the table is inserted as results.
function showResourceFigure_"""
                + t
                + """() {
    if ( $("#res_"""
                + t
                + """").length === 0) {
          dt = dt * 1.5; // slow-down checks for datatable as time goes on;
          setTimeout(showResourceFigure_"""
                + t
                + """, dt);
          return;
    } else {
        $("#res_"""
                + t
                + """").css('width', "100%").css('height', "300px");
        loadFiles(["http://www.flotcharts.org/flot/jquery.flot.js",
             "http://www.flotcharts.org/flot/jquery.flot.time.js"
            ], plotResourcePlot_"""
                + t
                + """);
    }
}
showResourceFigure_"""
                + t
                + """()
</script>
"""
            )
    elif verbosity == 0:
        print("\n".join(obtained_status))
    elif verbosity == 1:
        for s, (t, d) in zip(obtained_status, all_tasks):
            print(f"{t}\t{s}")
    elif verbosity == 2:
        tsize = 20
        for s, (t, d) in zip(obtained_status, all_tasks):
            ts, _, _, dr = TaskFile(t).tags_created_start_and_duration(
                formatted=not numeric_times
            )
            tsize = max(tsize, len(ts))
            print(f"{t}\t{ts.ljust(tsize)}\t{dr:<14}\t{s}")
    elif verbosity == 3:
        tsize = 20
        for s, (t, d) in zip(obtained_status, all_tasks):
            ts, ct, st, dr = TaskFile(t).tags_created_start_and_duration(
                formatted=not numeric_times
            )
            tsize = max(tsize, len(ts))
            print(f"{t}\t{ts.ljust(tsize)}\t{ct:<14}\t{st:<14}\t{dr:<14}\t{s}")
    elif verbosity == 4:
        import pprint

        for s, (t, d) in zip(obtained_status, all_tasks):
            tf = TaskFile(t)
            if s == "missing":
                print(f"{t}\t{s}\n")
                continue
            ts, ct, st, dr = tf.tags_created_start_and_duration(formatted=True)
            print(f"{t}\t{s}\n")
            print(f"{ct}")
            if st:
                print(f"{st}")
            if dr:
                print(f"{dr}")
            params = tf.params
            print("TASK:\n=====")
            if hasattr(params, "task_stack"):
                # show task of subtask
                print(f"#1 of {len(params.task_stack)} subtasks:")
                print(params.task_stack[0][1].task)
            else:
                print(params.task)
            print("TAGS:\n=====")
            print(tf.tags)
            print()
            if params.global_def:
                print("GLOBAL:\n=======")
                print(params.global_def)
                print()
            print("ENVIRONMENT:\n============")
            global_runtime = tf.runtime["_runtime"]
            job_vars = params.sos_dict
            job_vars["_runtime"].update(global_runtime)
            for k in sorted(job_vars.keys()):
                v = job_vars[k]
                print(f"{k:22}{short_repr(v) if verbosity == 3 else pprint.pformat(v)}")
            print()
            if tf.has_result():
                if s not in ("pending", "submitted", "running"):
                    res = tf.result
                    print("EXECUTION STATS:\n================")
                    if "start_time" in res and "end_time" in res:
                        print(
                            f'Duration:\t{format_duration(res["end_time"] - res["start_time"])}'
                        )
                    if "peak_cpu" in res:
                        print(f'Peak CPU:\t{res["peak_cpu"]*100} %')
                    if "peak_mem" in res:
                        print(f'Peak mem:\t{pretty_size(res["peak_mem"])}')

            elif s == "running":
                # we have separate pulse, out and err files
                pulse_file = os.path.join(
                    os.path.expanduser("~"), ".sos", "tasks", t + ".pulse"
                )
                if os.path.isfile(pulse_file):
                    print("EXECUTION STATS:\n================")
                    with open(pulse_file) as pulse:
                        print(summarizeExecution(t, pulse.read(), status=s))

            # if there are other files such as job file, print them.
            def show_file(task, exts):
                if isinstance(exts, str):
                    exts = [exts]
                for ext in exts:
                    f = os.path.join(
                        os.path.expanduser("~"), ".sos", "tasks", task + ext
                    )
                    if not os.path.isfile(f) or os.path.getsize(f) == 0:
                        return
                    print(
                        f'\n{os.path.basename(f)}:\n{"="*(len(os.path.basename(f))+1)}'
                    )
                    try:
                        with open(f) as fc:
                            print(fc.read())
                    except Exception:
                        print("Binary file")

            if s == "running":
                show_file(t, ".sh")
                show_file(t, ".job_id")
                show_file(t, [".sosout", ".out"])
                show_file(t, [".soserr", ".err"])
            elif s == "submitted":
                show_file(t, ".sh")
                show_file(t, ".job_id")
            elif s != "pending":
                if tf.has_shell():
                    print("\nexecution script:\n================\n" + tf.shell)
                else:
                    show_file(t, ".sh")
                if tf.has_stdout():
                    print("\nstandard output:\n================\n" + tf.stdout)
                else:
                    show_file(t, [".sosout", ".out"])
                if tf.has_stderr():
                    print("\nstandard error:\n================\n" + tf.stderr)
                else:
                    show_file(t, [".soserr", ".err"])

    # remove jobs that are older than 1 month
    if to_be_removed:
        purge_tasks(to_be_removed, verbosity=0)


def kill_tasks(tasks, tags=None):
    #
    import glob
    from multiprocessing.pool import ThreadPool as Pool

    if not tasks:
        tasks = glob.glob(
            os.path.join(os.path.expanduser("~"), ".sos", "tasks", "*.task")
        )
        all_tasks = [os.path.basename(x)[:-5] for x in tasks]
    else:
        all_tasks = []
        for t in tasks:
            matched = glob.glob(
                os.path.join(os.path.expanduser("~"), ".sos", "tasks", f"{t}*.task")
            )
            matched = [os.path.basename(x)[:-5] for x in matched]
            if not matched:
                env.logger.warning(f"{t} does not match any existing task")
            else:
                all_tasks.extend(matched)
    if tags:
        all_tasks = [
            x for x in all_tasks if any(x in tags for x in TaskFile(x).tags.split())
        ]

    if not all_tasks:
        env.logger.debug("No task to kill")
        return
    all_tasks = sorted(list(set(all_tasks)))
    # at most 20 threads
    p = Pool(min(20, len(all_tasks)))
    killed = p.map(kill_task, all_tasks)
    for s, t in zip(killed, all_tasks):
        print(f"{t}\t{s}")


def kill_task(task):
    tf = TaskFile(task)
    status = tf.status
    if status == "completed":
        return "completed"
    with open(
        os.path.join(os.path.expanduser("~"), ".sos", "tasks", task + ".soserr"), "a"
    ) as err:
        err.write(f"Task {task} killed by sos kill command or task engine.")
    tf.add_outputs()
    # 1323
    tf.add_result()

    TaskFile(task).status = "aborted"
    remove_task_files(
        task, [".sosout", ".soserr", ".out", ".err", ".pulse", ".sh", ".job_id"]
    )
    return "aborted"


def purge_tasks(tasks, purge_all=None, age=None, status=None, tags=None, verbosity=2):
    # verbose is ignored for now
    # if not tasks and not purge_all:
    #     # if not --all and no task is specified, find all tasks in the current directory
    #     from .signatures import WorkflowSignatures
    #     workflow_signatures = WorkflowSignatures()
    #     tasks = [
    #         x for x in workflow_signatures.tasks() if os.path.isfile(
    #             os.path.join(
    #                 os.path.expanduser('~'), '.sos', 'tasks', x + '.task'))
    #     ]
    import glob

    if tasks:
        all_tasks = []
        for t in tasks:
            matched = glob.glob(
                os.path.join(os.path.expanduser("~"), ".sos", "tasks", f"{t}*.task")
            )
            matched = [(os.path.basename(x)[:-5], os.path.getmtime(x)) for x in matched]
            if not matched:
                print(f"{t}\tmissing")
            all_tasks.extend(matched)
    elif purge_all or age or status or tags:
        tasks = glob.glob(
            os.path.join(os.path.expanduser("~"), ".sos", "tasks", "*.task")
        )
        all_tasks = [(os.path.basename(x)[:-5], os.path.getmtime(x)) for x in tasks]
    else:
        raise ValueError(
            "Please specify either tasks or one or more of --all, --status, --tags--age"
        )
    #
    if age is not None:
        age = expand_time(age, default_unit="d")
        if age > 0:
            all_tasks = [x for x in all_tasks if time.time() - x[1] >= age]
        else:
            all_tasks = [x for x in all_tasks if time.time() - x[1] <= -age]

    if status:
        # at most 20 threads
        task_status = check_tasks([x[0] for x in all_tasks], not tasks)
        all_tasks = [x for x in all_tasks if task_status[x[0]]["status"] in status]

    if tags:
        all_tasks = [
            x for x in all_tasks if any(x in tags for x in TaskFile(x[0]).tags.split())
        ]
    #
    # remoe all task files
    all_tasks = set([x[0] for x in all_tasks])
    if all_tasks:
        #
        # find all related files, including those in nested directories
        from collections import defaultdict

        to_be_removed = defaultdict(list)
        for dirname, _, filelist in os.walk(
            os.path.join(os.path.expanduser("~"), ".sos", "tasks")
        ):
            for f in filelist:
                ID = os.path.basename(f).split(".", 1)[0]
                if ID in all_tasks:
                    to_be_removed[ID].append(os.path.join(dirname, f))
        #
        cache_file: str = os.path.join(
            os.path.expanduser("~"), ".sos", "tasks", "status_cache.pickle"
        )

        if os.path.isfile(cache_file):
            with fasteners.InterProcessLock(cache_file + "_"):
                with open(cache_file, "rb") as cache:
                    status_cache = pickle.load(cache)
        else:
            status_cache = {}
        for task in all_tasks:
            removed = True
            for f in to_be_removed[task]:
                try:
                    if verbosity > 3:
                        if (
                            "TASK" in env.config["SOS_DEBUG"]
                            or "ALL" in env.config["SOS_DEBUG"]
                        ):
                            env.log_to_file("TASK", f"Remove {f}")
                    os.remove(f)
                except Exception as e:
                    removed = False
                    if verbosity > 0:
                        env.logger.warning(f"Failed to purge task {task[0]}: {e}")
            status_cache.pop(task, None)
            if removed and verbosity > 1:
                print(f"{task}\tpurged")
        with fasteners.InterProcessLock(cache_file + "_"):
            with open(cache_file, "wb") as cache:
                pickle.dump(status_cache, cache)
    elif verbosity > 1:
        env.logger.debug("No matching tasks to purge")
    if purge_all and age is None and status is None and tags is None:
        matched = glob.glob(os.path.join(os.path.expanduser("~"), ".sos", "tasks", "*"))
        count = 0
        for f in matched:
            if os.path.isdir(f):
                import shutil

                try:
                    shutil.rmtree(f)
                    count += 1
                except Exception as e:
                    if verbosity > 0:
                        env.logger.warning(f"Failed to remove {f}: {e}")
            else:
                try:
                    os.remove(f)
                    count += 1
                except Exception as e:
                    if verbosity > 0:
                        env.logger.warning(f"Failed to remove {e}")
        if count > 0 and verbosity > 1:
            env.logger.info(f"{count} other files and directories are removed.")
    return ""
