#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.
import copy
import os
import subprocess
import pickle
import time
import zmq
import signal

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from contextlib import redirect_stdout, redirect_stderr
from threading import Event

from .eval import SoS_eval, SoS_exec
from .monitor import ProcessMonitor
from .targets import (
    InMemorySignature,
    file_target,
    sos_step,
    dynamic,
    path,
    sos_targets,
)
from .utils import StopInputGroup, env, pickleable, ProcessKilled, get_localhost_ip
from .tasks import (
    TaskFile,
    combine_results,
    remove_task_files,
    monitor_interval,
    resource_monitor_interval,
)
from .step_executor import parse_shared_vars
from .messages import decode_msg
from .executor_utils import __null_func__, get_traceback_msg, prepare_env, clear_output
from .controller import (
    Controller,
    connect_controllers,
    disconnect_controllers,
    create_socket,
    close_socket,
    request_answer_from_controller,
    send_message_to_controller,
)


def signal_handler(*args, **kwargs):
    raise ProcessKilled()


class BaseTaskExecutor(object):
    """Task executor used to execute specified tasks. Any customized task executor
    should derive from this class.
    """

    def __init__(self):
        pass

    def execute(self, task_id):
        """Execute single or master task, return a dictionary"""
        tf = TaskFile(task_id)

        # this will automatically create a pulse file
        tf.status = "running"
        # write result file
        try:
            signal.signal(signal.SIGTERM, signal_handler)
            # open task file and get parameters
            params, runtime = tf.get_params_and_runtime()
            sig_content = tf.signature

            if env.config["verbosity"] is not None:
                env.verbosity = env.config["verbosity"]

            if env.config["run_mode"] == "dryrun":
                env.config["sig_mode"] = "ignore"

            env.logger.info(f"{task_id} ``started``")

            # the runtime consists of two parts, one is defined by the task (e.g.
            # parameter walltime) and saved in params.sos_dict['_runtime'], another
            # is defined by the task engine (e.g. max_walltime) saved in run_time.
            # max_walltime, max_mem, max_procs can only be defined in task_engine

            # let us make sure _runtime exists to avoid repeated check
            if "_runtime" not in params.sos_dict:
                params.sos_dict["_runtime"] = {}
            if "_runtime" not in runtime:
                runtime["_runtime"] = {}

            m = ProcessMonitor(
                task_id,
                monitor_interval=monitor_interval,
                resource_monitor_interval=resource_monitor_interval,
                max_walltime=runtime["_runtime"].get("max_walltime", None),
                max_mem=runtime["_runtime"].get("max_mem", None),
                max_procs=runtime["_runtime"].get("max_procs", None),
                sos_dict=params.sos_dict,
            )

            m.start()

            if hasattr(params, "task_stack"):
                res = self.execute_master_task(task_id, params, runtime, sig_content)
            else:
                res = self.execute_single_task(task_id, params, runtime, sig_content)
        except KeyboardInterrupt:
            tf.status = "aborted"
            raise
        except ProcessKilled:
            tf.status = "aborted"
            raise ProcessKilled("task interrupted")
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)

        if res["ret_code"] != 0 and "exception" in res:
            with open(
                os.path.join(
                    os.path.expanduser("~"), ".sos", "tasks", task_id + ".soserr"
                ),
                "a",
            ) as err:
                err.write(f'Task {task_id} exits with code {res["ret_code"]}')

        if res.get("skipped", False):
            # a special mode for skipped to set running time to zero
            tf.status = "skipped"
        else:
            tf.add_outputs()
            tf.add_result(res)

            # this will remove pulse and other files
            tf.status = "completed" if res["ret_code"] == 0 else "failed"

        return res["ret_code"]

    def execute_single_task(
        self, task_id, params, runtime, sig_content, quiet=False, **kwargs
    ):
        """
        Execute a single task, with

        task_id: id of the task, a string
        params: task definitions, with global_def, task, and sos_dict as its members.
        runtime: can have key '_runtime' for task specific runtime, and task_id for task specific variables
        sig_content: existing signatures

        All other task executors calls this function eventually.
        """
        # this is task specific runtime information, used to update global _runtime
        params.sos_dict["_runtime"].update(runtime["_runtime"])
        # this is subtask dictionary
        if task_id in runtime:
            params.sos_dict.update(runtime[task_id])

        if (
            quiet
            and "TASK" in env.config["SOS_DEBUG"]
            or "ALL" in env.config["SOS_DEBUG"]
        ):
            env.log_to_file("TASK", f"Executing task {task_id}")

        global_def, task, sos_dict = params.global_def, params.task, params.sos_dict

        prepare_env(global_def[0], global_def[1])

        env.sos_dict.quick_update(sos_dict)

        for key in [
            "step_input",
            "_input",
            "step_output",
            "_output",
            "step_depends",
            "_depends",
        ]:
            if key in sos_dict and isinstance(sos_dict[key], sos_targets):
                # resolve remote() target
                env.sos_dict.set(
                    key, sos_dict[key].remove_targets(type=sos_step).resolve_remote()
                )

        # when no output is specified, we just treat the task as having no output (determined)
        env.sos_dict["_output"]._undetermined = False
        sig = (
            None
            if env.config["sig_mode"] == "ignore"
            else InMemorySignature(
                env.sos_dict["_input"],
                env.sos_dict["_output"],
                env.sos_dict["_depends"],
                env.sos_dict["__signature_vars__"],
                shared_vars=parse_shared_vars(
                    env.sos_dict["_runtime"].get("shared", None)
                ),
            )
        )

        if sig and self._validate_task_signature(
            sig, sig_content.get(task_id, {}), task_id, quiet
        ):
            # env.logger.info(f'{task_id} ``skipped``')
            return self._collect_task_result(
                task_id, sos_dict, params.tags, skipped=True, signature=sig
            )

        # if we are to really execute the task, touch the task file so that sos status shows correct
        # execution duration.
        if not quiet:
            sos_dict["start_time"] = time.time()

        # task output
        env.sos_dict.set(
            "__std_out__",
            os.path.join(os.path.expanduser("~"), ".sos", "tasks", task_id + ".sosout"),
        )
        env.sos_dict.set(
            "__std_err__",
            os.path.join(os.path.expanduser("~"), ".sos", "tasks", task_id + ".soserr"),
        )
        env.logfile = os.path.join(
            os.path.expanduser("~"), ".sos", "tasks", task_id + ".soserr"
        )
        # clear the content of existing .out and .err file if exists, but do not create one if it does not exist
        if os.path.exists(env.sos_dict["__std_out__"]):
            open(env.sos_dict["__std_out__"], "w").close()
        if os.path.exists(env.sos_dict["__std_err__"]):
            open(env.sos_dict["__std_err__"], "w").close()

        try:
            orig_dir = os.getcwd()
            # go to 'workdir'
            if "workdir" in sos_dict["_runtime"]:
                workdir = path(sos_dict["_runtime"]["workdir"])
                if not os.path.isdir(workdir):
                    try:
                        os.makedirs(workdir)
                        os.chdir(workdir)
                    except Exception as e:
                        # sometimes it is not possible to go to a "workdir" because of
                        # file system differences, but this should be ok if a work_dir
                        # has been specified.
                        env.logger.debug(
                            f'Failed to create workdir {sos_dict["_runtime"]["workdir"]}: {e}'
                        )
                else:
                    os.chdir(workdir)

            # we will need to check existence of targets because the task might
            # be executed on a remote host where the targets are not available.
            for target in (
                sos_dict["_input"] if isinstance(sos_dict["_input"], list) else []
            ) + (
                sos_dict["_depends"] if isinstance(sos_dict["_depends"], list) else []
            ):
                # if the file does not exist (although the signature exists)
                # request generation of files
                if isinstance(target, str):
                    if not file_target(target).target_exists("target"):
                        # remove the signature and regenerate the file
                        raise RuntimeError(f"{target} not found")
                # the sos_step target should not be checked in tasks because tasks are
                # independently executable units.
                elif not isinstance(target, sos_step) and not target.target_exists(
                    "target"
                ):
                    raise RuntimeError(f"{target} not found")

            # create directory. This usually has been done at the step level but the task can be executed
            # on a remote host where the directory does not yet exist.
            ofiles = env.sos_dict["_output"]
            if ofiles.valid():
                for ofile in ofiles:
                    parent_dir = ofile.parent
                    if not parent_dir.is_dir():
                        parent_dir.mkdir(parents=True, exist_ok=True)

            # set environ ...
            # we join PATH because the task might be executed on a different machine
            if "env" in sos_dict["_runtime"]:
                for key, value in sos_dict["_runtime"]["env"].items():
                    if "PATH" in key and key in os.environ:
                        new_path = OrderedDict()
                        for p in value.split(os.pathsep):
                            new_path[p] = 1
                        for p in value.split(os.environ[key]):
                            new_path[p] = 1
                        os.environ[key] = os.pathsep.join(new_path.keys())
                    else:
                        os.environ[key] = value
            if "prepend_path" in sos_dict["_runtime"]:
                if isinstance(sos_dict["_runtime"]["prepend_path"], str):
                    os.environ["PATH"] = (
                        sos_dict["_runtime"]["prepend_path"]
                        + os.pathsep
                        + os.environ["PATH"]
                    )
                elif isinstance(env.sos_dict["_runtime"]["prepend_path"], Sequence):
                    os.environ["PATH"] = (
                        os.pathsep.join(sos_dict["_runtime"]["prepend_path"])
                        + os.pathsep
                        + os.environ["PATH"]
                    )
                else:
                    raise ValueError(
                        f'Unacceptable input for option prepend_path: {sos_dict["_runtime"]["prepend_path"]}'
                    )

            with open(env.sos_dict["__std_out__"], "a") as my_stdout, open(
                env.sos_dict["__std_err__"], "a"
            ) as my_stderr:
                with redirect_stdout(my_stdout), redirect_stderr(my_stderr):
                    # step process
                    SoS_exec(task)

                if "logfile" in params.sos_dict["_runtime"]:
                    logfile = params.sos_dict["_runtime"]["logfile"]
                    if not os.path.isfile(logfile):
                        raise ValueError(
                            f"logfile {logfile} does not exist after the completion of task"
                        )
                    try:
                        with open(logfile, "r") as log:
                            my_stdout.write(f"logfile: {logfile}\n")
                            my_stdout.write(log.read())
                    except Exception as e:
                        raise ValueError(
                            f"Failed to collect logfile {logfile} after the completion of task: {e}"
                        )

            if quiet or env.config["run_mode"] != "run":
                env.logger.debug(f"{task_id} ``completed``")
            else:
                env.logger.info(f"{task_id} ``completed``")

        except StopInputGroup as e:
            # task ignored with stop_if exception
            if not e.keep_output:
                clear_output()
                env.sos_dict["_output"] = sos_targets([])
            if e.message:
                env.logger.info(e.message)
            return {
                "ret_code": 0,
                "task": task_id,
                "tags": params.tags,
                "input": sos_targets([]),
                "output": env.sos_dict["_output"],
                "depends": sos_targets([]),
                "shared": {},
            }
        except KeyboardInterrupt:
            env.logger.error(f"{task_id} ``interrupted``")
            raise
        except subprocess.CalledProcessError as e:
            return {
                "ret_code": e.returncode,
                "task": task_id,
                "tags": params.tags,
                "shared": {},
                "exception": RuntimeError(e.stderr),
            }
        except ProcessKilled:
            env.logger.error(f"{task_id} ``interrupted``")
            raise
        except Exception as e:
            msg = get_traceback_msg(e)
            # env.logger.error(f'{task_id} ``failed``: {msg}')
            with open(
                os.path.join(
                    os.path.expanduser("~"), ".sos", "tasks", task_id + ".soserr"
                ),
                "a",
            ) as err:
                err.write(msg + "\n")
            return {
                "ret_code": 1,
                "exception": RuntimeError(msg),
                "task": task_id,
                "tags": params.tags,
                "shared": {},
            }
        finally:
            os.chdir(orig_dir)

        return self._collect_task_result(task_id, sos_dict, params.tags, signature=sig)

    def execute_master_task(self, task_id, params, master_runtime, sig_content):
        """
        Execute a master task with multiple subtasks.

        task_id; id of master task.
        params: master parameters, with params.task_stack having params and
            runtime of subtasks
        master_runtime: master runtime, with runtime supplemented by subtasks
        sig_content: master signature with signatures for all subtasks.

        """
        # used by self._collect_subtask_outputs
        self.master_stdout = os.path.join(
            os.path.expanduser("~"), ".sos", "tasks", task_id + ".sosout"
        )
        self.master_stderr = os.path.join(
            os.path.expanduser("~"), ".sos", "tasks", task_id + ".soserr"
        )

        if os.path.exists(self.master_stdout):
            open(self.master_stdout, "w").close()
        if os.path.exists(self.master_stderr):
            open(self.master_stderr, "w").close()

        # options specified from command line, most likely from cluster system
        #
        # As explained in ticket 1304:
        #
        # From the point of cluster system, this is only one "job" on each
        # node. The "n_procs" is the "number of cpus per job per node", which,
        # is "cores" (per task) * "n_workers" (per node). Therefore,
        # "n_procs" should not be used.
        n_nodes, n_procs = self._parse_num_workers(env.config["worker_procs"])

        # regular trunk_workers = ?? (0 was used as default)
        # a previous version of master task file has params.num_workers
        n_workers = (
            params.num_workers
            if hasattr(params, "num_workers")
            else params.sos_dict["_runtime"].get("num_workers", 1)
        )

        if isinstance(n_workers, int):
            if n_workers > 1:
                results = self.execute_master_task_in_parallel(
                    params, master_runtime, sig_content, n_workers
                )
            else:
                # n_workers = 1
                results = self.execute_master_task_sequentially(
                    params, master_runtime, sig_content
                )
        elif n_nodes == 1:
            if n_workers is None:
                n_workers = 1
            elif isinstance(n_workers, Sequence):
                # single node, so n_workers can have at most 1 element
                n_workers = n_workers[0]
            elif not isinstance(n_workers, int):
                raise ValueError(f"Illegal number of workers {n_workers}")

            if n_workers == 1:
                results = self.execute_master_task_sequentially(
                    params, master_runtime, sig_content
                )
            else:
                results = self.execute_master_task_in_parallel(
                    params, master_runtime, sig_content, n_workers
                )
        else:
            # n_workers should be a sequence
            if not n_workers:
                n_workers = env.config["worker_procs"]
            elif len(n_workers) != n_nodes:
                env.logger.warning(
                    f'task options trunk_workers={n_workers} is inconsistent with command line option -j {env.config["worker_procs"]}'
                )
            results = self.execute_master_task_distributedly(
                params, master_runtime, sig_content, n_workers
            )

        return combine_results(task_id, results)

    def execute_master_task_in_parallel(
        self, params, master_runtime, sig_content, n_workers
    ):
        # multiple workers, concurrent execution using a pool
        from multiprocessing.pool import Pool

        p = Pool(n_workers)
        results = []
        for sub_id, sub_params in params.task_stack:
            if hasattr(params, "common_dict"):
                sub_params.sos_dict.update(params.common_dict)
            sub_runtime = {x: master_runtime.get(x, {}) for x in ("_runtime", sub_id)}
            sub_sig = {sub_id: sig_content.get(sub_id, {})}
            res = p.apply_async(
                self.execute_single_task,
                (sub_id, sub_params, sub_runtime, sub_sig, True),
                callback=self._append_subtask_outputs,
            )
            results.append(res)

        for idx, r in enumerate(results):
            results[idx] = r.get()
            self._cache_subresult(params.ID, results[idx])
        p.close()
        p.join()
        return results

    def execute_master_task_sequentially(self, params, master_runtime, sig_content):
        # single worker, execute sequentially, n_workers is not a positive number
        results = []
        for sub_id, sub_params in params.task_stack:
            if hasattr(params, "common_dict"):
                sub_params.sos_dict.update(params.common_dict)
            sub_runtime = {x: master_runtime.get(x, {}) for x in ("_runtime", sub_id)}
            sub_sig = {sub_id: sig_content.get(sub_id, {})}
            res = self.execute_single_task(
                sub_id, sub_params, sub_runtime, sub_sig, True
            )
            try:
                self._append_subtask_outputs(res)
            except Exception as e:
                env.logger.warning(f"Failed to copy result of subtask {sub_id}: {e}")
            self._cache_subresult(params.ID, res)
            results.append(res)
        return results

    def execute_master_task_distributedly(
        self, params, master_runtime, sig_content, n_workers
    ):
        # multiple workers, start workers from remote hosts
        env.zmq_context = zmq.Context()

        # control panel in a separate thread, connected by zmq socket
        ready = Event()
        self.controller = Controller(ready)
        self.controller.start()
        # wait for the thread to start with a signature_req saved to env.config
        ready.wait()

        connect_controllers(env.zmq_context)

        try:

            # start a result receving socket
            self.result_pull_socket = create_socket(
                env.zmq_context, zmq.PULL, "substep result collector"
            )
            local_ip = get_localhost_ip()
            port = self.result_pull_socket.bind_to_random_port(f"tcp://{local_ip}")
            env.config["sockets"]["result_push_socket"] = f"tcp://{local_ip}:{port}"

            # send tasks to the controller
            results = []
            for sub_id, sub_params in params.task_stack:
                if hasattr(params, "common_dict"):
                    sub_params.sos_dict.update(params.common_dict)
                sub_runtime = {
                    x: master_runtime.get(x, {}) for x in ("_runtime", sub_id)
                }
                sub_sig = {sub_id: sig_content.get(sub_id, {})}

                # submit tasks
                send_message_to_controller(
                    [
                        "task",
                        dict(
                            task_id=sub_id,
                            params=sub_params,
                            runtime=sub_runtime,
                            sig_content=sub_sig,
                            config=env.config,
                            quiet=True,
                        ),
                    ]
                )

            for idx in range(len(params.task_stack)):
                res = decode_msg(self.result_pull_socket.recv())
                try:
                    self._append_subtask_outputs(res)
                except Exception as e:
                    env.logger.warning(f"Failed to copy result of subtask: {e}")
                self._cache_subresult(params.ID, res)
                results.append(res)
            succ = True
        except Exception as e:
            env.logger.error(f"Failed to execute task {params.ID}: {e}")
            succ = False
        finally:
            # end progress bar when the master workflow stops
            close_socket(self.result_pull_socket)
            env.log_to_file("EXECUTOR", f"Stop controller from {os.getpid()}")
            request_answer_from_controller(["done", succ])
            env.log_to_file("EXECUTOR", "disconntecting master")
            self.controller.join()
            disconnect_controllers(env.zmq_context if succ else None)
        return results

    def _cache_subresult(self, master_id, sub_result):
        cache_file = os.path.join(
            os.path.expanduser("~"), ".sos", "tasks", master_id + ".cache"
        )
        with open(cache_file, "ab") as cache:
            pickle.dump(sub_result, cache)

    def _parse_num_workers(self, num_workers):
        # return number of nodes and workers
        if isinstance(num_workers, Sequence):
            if len(num_workers) >= 1:
                val = num_workers[0]
                if ":" in val:
                    val = val.rsplit(":", 1)[-1]
                n_workers = int(val.rsplit(":", 1)[-1])
                return len(num_workers), None if n_workers <= 0 else n_workers
            else:
                return None, None
        elif isinstance(num_workers, str):
            if ":" in num_workers:
                num_workers = num_workers.rsplit(":", 1)[-1]
            n_workers = int(num_workers.rsplit(":", 1)[-1])
            return 1, None if n_workers <= 0 else n_workers
        elif isinstance(num_workers, int) and num_workers >= 1:
            return 1, num_workers
        elif num_workers is None:
            return None, None
        else:
            raise RuntimeError(
                f"Unacceptable value for parameter trunk_workers {num_workers}"
            )

    def _append_subtask_outputs(self, result):
        """
        Append result returned from subtask to stdout and stderr streams
        """
        tid = result["task"]
        tags = " ".join(result.get("tags", []))
        with open(self.master_stdout, "ab") as out, open(
            self.master_stderr, "ab"
        ) as err:
            dest = out if result["ret_code"] == 0 else err
            dest.write(
                f'> {tid}\t{tags}\t{"completed" if result["ret_code"] == 0 else "failed"}\n'.encode()
            )
            if "output" in result and result["output"]:
                dest.write(f'output files:\n{result["output"]}\n'.encode())
            sub_out = os.path.join(
                os.path.expanduser("~"), ".sos", "tasks", tid + ".sosout"
            )
            if os.path.isfile(sub_out):
                with open(sub_out, "rb") as sout:
                    out_content = sout.read()
                    if out_content:
                        dest.write(b"stdout:\n")
                        dest.write(out_content)
                try:
                    os.remove(sub_out)
                except Exception as e:
                    env.logger.warning(f"Failed to remove {sub_out}: {e}")

            sub_err = os.path.join(
                os.path.expanduser("~"), ".sos", "tasks", tid + ".soserr"
            )
            if "exception" in result:
                dest.write(str(result["exception"]).encode())
            # err.write(
            #     f'{tid}: {"completed" if result["ret_code"] == 0 else "failed"}\n'
            #     .encode())
            if os.path.isfile(sub_err):
                with open(sub_err, "rb") as serr:
                    err_content = serr.read()
                    if err_content:
                        dest.write(b"stderr:\n")
                        dest.write(err_content)
                try:
                    os.remove(sub_err)
                except Exception as e:
                    env.logger.warning(f"Failed to remove {sub_err}: {e}")
            dest.write(b"\n")
        # remove other files as well
        try:
            remove_task_files(tid, [".sosout", ".soserr", ".out", ".err"])
        except Exception as e:
            env.logger.debug(f"Failed to remove files {tid}: {e}")

    def _collect_task_result(
        self, task_id, sos_dict, tags, skipped=False, signature=None
    ):
        shared = {}
        if "shared" in env.sos_dict["_runtime"]:
            svars = env.sos_dict["_runtime"]["shared"]
            if isinstance(svars, str):
                if svars not in env.sos_dict:
                    raise ValueError(
                        f"Unavailable shared variable {svars} after the completion of task {task_id}"
                    )
                if not pickleable(env.sos_dict[svars], svars):
                    env.logger.warning(
                        f"{svars} of type {type(env.sos_dict[svars])} is not sharable"
                    )
                else:
                    shared[svars] = copy.deepcopy(env.sos_dict[svars])
            elif isinstance(svars, Mapping):
                for var, val in svars.items():
                    if var != val:
                        env.sos_dict.set(var, SoS_eval(val))
                    if var not in env.sos_dict:
                        raise ValueError(
                            f"Unavailable shared variable {var} after the completion of task {task_id}"
                        )
                    if not pickleable(env.sos_dict[var], var):
                        env.logger.warning(
                            f"{var} of type {type(env.sos_dict[var])} is not sharable"
                        )
                    else:
                        shared[var] = copy.deepcopy(env.sos_dict[var])
            elif isinstance(svars, Sequence):
                # if there are dictionaries in the sequence, e.g.
                # shared=['A', 'B', {'C':'D"}]
                for item in svars:
                    if isinstance(item, str):
                        if item not in env.sos_dict:
                            raise ValueError(
                                f"Unavailable shared variable {item} after the completion of task {task_id}"
                            )
                        if not pickleable(env.sos_dict[item], item):
                            env.logger.warning(
                                f"{item} of type {type(env.sos_dict[item])} is not sharable"
                            )
                        else:
                            shared[item] = copy.deepcopy(env.sos_dict[item])
                    elif isinstance(item, Mapping):
                        for var, val in item.items():
                            if var != val:
                                env.sos_dict.set(var, SoS_eval(val))
                            if var not in env.sos_dict:
                                raise ValueError(
                                    f"Unavailable shared variable {var} after the completion of task {task_id}"
                                )
                            if not pickleable(env.sos_dict[var], var):
                                env.logger.warning(
                                    f"{var} of type {type(env.sos_dict[var])} is not sharable"
                                )
                            else:
                                shared[var] = copy.deepcopy(env.sos_dict[var])
                    else:
                        raise ValueError(
                            f"Option shared should be a string, a mapping of expression, or a list of string or mappings. {svars} provided"
                        )
            else:
                raise ValueError(
                    f"Option shared should be a string, a mapping of expression, or a list of string or mappings. {svars} provided"
                )
            env.log_to_file(
                "TASK",
                f'task {task_id} (index={env.sos_dict["_index"]}) return shared variable {shared}',
            )

        # the difference between sos_dict and env.sos_dict is that sos_dict (the original version) can have remote() targets
        # which should not be reported.
        if env.sos_dict["_output"].undetermined():
            # re-process the output statement to determine output files
            args, _ = SoS_eval(
                f'__null_func__({env.sos_dict["_output"]._undetermined})',
                extra_dict={"__null_func__": __null_func__},
            )
            # handle dynamic args
            env.sos_dict.set(
                "_output",
                sos_targets(
                    [x.resolve() if isinstance(x, dynamic) else x for x in args]
                ),
            )

        return {
            "ret_code": 0,
            "task": task_id,
            "tags": tags,
            "input": sos_dict["_input"],
            "output": sos_dict["_output"],
            "depends": sos_dict["_depends"],
            "shared": shared,
            "skipped": skipped,
            "start_time": sos_dict.get("start_time", ""),
            "peak_cpu": sos_dict.get("peak_cpu", 0),
            "peak_mem": sos_dict.get("peak_mem", 0),
            "end_time": time.time(),
            "signature": {task_id: signature.write()} if signature else {},
        }

    def _validate_task_signature(self, sig, saved_sig, task_id, is_subtask):
        idx = env.sos_dict["_index"]
        if env.config["sig_mode"] in ("default", "skip", "distributed"):
            matched = sig.validate(saved_sig)
            if isinstance(matched, dict):
                # in this case, an Undetermined output can get real output files
                # from a signature
                env.sos_dict.set("_input", sos_targets(matched["input"]))
                env.sos_dict.set("_depends", sos_targets(matched["depends"]))
                env.sos_dict.set("_output", sos_targets(matched["output"]))
                env.sos_dict.update(matched["vars"])
                if is_subtask or env.config["run_mode"] != "run":
                    env.logger.debug(
                        f'Task ``{task_id}`` for substep ``{env.sos_dict["step_name"]}`` (index={idx}) is ``ignored`` due to saved signature'
                    )
                else:
                    env.logger.info(
                        f'Task ``{task_id}`` for substep ``{env.sos_dict["step_name"]}`` (index={idx}) is ``ignored`` due to saved signature'
                    )
                return True
        elif env.config["sig_mode"] == "assert":
            matched = sig.validate(saved_sig)
            if isinstance(matched, str):
                raise RuntimeError(f"Signature mismatch: {matched}")
            env.sos_dict.set("_input", sos_targets(matched["input"]))
            env.sos_dict.set("_depends", sos_targets(matched["depends"]))
            env.sos_dict.set("_output", sos_targets(matched["output"]))
            env.sos_dict.update(matched["vars"])

            if is_subtask or env.config["run_mode"] != "run":
                env.logger.debug(
                    f'Task ``{task_id}`` for substep ``{env.sos_dict["step_name"]}`` (index={idx}) is ``ignored`` with matching signature'
                )
            else:
                env.logger.info(
                    f'Task ``{task_id}`` for substep ``{env.sos_dict["step_name"]}`` (index={idx}) is ``ignored`` with matching signature'
                )
            sig.content = saved_sig
            return True
        elif env.config["sig_mode"] == "build":
            # The signature will be write twice
            if sig.write():
                if is_subtask or env.config["run_mode"] != "run":
                    env.logger.debug(
                        f'Task ``{task_id}`` for substep ``{env.sos_dict["step_name"]}`` (index={idx}) is ``ignored`` with signature constructed'
                    )
                else:
                    env.logger.info(
                        f'Task ``{task_id}`` for substep ``{env.sos_dict["step_name"]}`` (index={idx}) is ``ignored`` with signature constructed'
                    )
                return True
            return False
        elif env.config["sig_mode"] == "force":
            return False
        else:
            raise RuntimeError(f'Unrecognized signature mode {env.config["sig_mode"]}')
