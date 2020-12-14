#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.
import glob
import multiprocessing as mp
import os
import shutil
import base64
import socket
import stat
import subprocess
import sys
import time
import pexpect
from collections.abc import Sequence

import pkg_resources

from .eval import Undetermined, cfg_interpolate, get_config
from .syntax import SOS_LOGLINE
from .targets import path, sos_targets
from .task_engines import BackgroundProcess_TaskEngine
from .workflow_engines import BackgroundProcess_WorkflowEngine
from .tasks import TaskFile
from .utils import env, expand_size, expand_time, format_HHMMSS, short_repr

#
# A 'queue' is defined by queue configurations in SoS configuration files.
# It encapsulate properties of a queue and tells sos how to interact with
# the queue. A queue can be a local host, a remote host without queue, or
# a remote host with a task queue, or a RQ or Celery server. There are
# two main categories of properties of a host.
#
# 1. host properties, namely how to copy files and how to execute comamnds
#   on the host. Note that even for queues that communicate with sos
#   through network, the workers might be on a different host with different
#   file systems.
#
#   Keys for host configuration include:
#   * path_map: path map between local and remote hosts
#   * shared: paths that are shared between local and remote hosts
#   * send_cmd (optional): alternative command to send files
#   * receive_cmd (optional): alternative command to receive files
#   * execute_cmd (optional): alternative command to execute commands
#
# 2. task properties, namely how to manage running jobs. These include
#   direct execution, PBS and various cluster systems, and various task
#   queues.
#
#   Keys for task configuration depend largely one queue type.
#
#   * task_engine: type of task engine
#   * max_jobs: maximum number of concurrent jobs on the host.
#
#
# Implementation wise, a queue instance is created for each queue.
#

from typing import Any, Dict, List, Optional, Union


class DaemonizedProcess(mp.Process):
    def __init__(self, cmd, *args, **kwargs):
        super(DaemonizedProcess, self).__init__(*args, **kwargs)
        self.cmd = cmd

    def run(self):
        try:
            pid = os.fork()
            if pid > 0:
                # exit from second parent
                sys.exit(0)
        except OSError as err:
            env.logger.error(f"_Fork #1 failed: {err}\n")
            sys.exit(1)

        os.setsid()
        os.umask(0)
        # do second fork
        try:
            pid = os.fork()
            if pid > 0:
                # exit from second parent
                sys.exit(0)
        except OSError as err:
            env.logger.error(f"_Fork #2 failed: {err}\n")
            sys.exit(1)
        # the following is also need to properly daemonize the process
        # redirect standard file descriptors
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            si = open(os.devnull, "r")
            so = open(os.devnull, "w")
            se = open(os.devnull, "w")
            os.dup2(si.fileno(), sys.stdin.fileno())
            os.dup2(so.fileno(), sys.stdout.fileno())
            os.dup2(se.fileno(), sys.stderr.fileno())
        except Exception:
            # #493
            pass

        # fork a new process
        subprocess.Popen(self.cmd, shell=True, close_fds=True)
        return


def _show_err_and_out(task_id, res) -> None:
    if "stdout" in res:
        sys.stderr.write(f"\n{task_id}.out:\n")
        ends_with_newline = False
        for line in res["stdout"].splitlines():
            if not SOS_LOGLINE.match(line):
                sys.stderr.write(line)
                ends_with_newline = line.endswith("\n")
        if not ends_with_newline:
            sys.stderr.write("\n")
    if "stderr" in res:
        sys.stderr.write(f"\n{task_id}.err:\n")
        ends_with_newline = False
        for line in res["stderr"].splitlines():
            if not SOS_LOGLINE.match(line):
                sys.stderr.write(line)
                ends_with_newline = line.endswith("\n")
        if not ends_with_newline:
            sys.stderr.write("\n")


class LocalHost(object):
    """For local host, no path map, send and receive ..."""

    def __init__(
        self,
        config: Dict[str, Union[str, int, List[str]]],
        test_connection: bool = True,
    ) -> None:
        super(LocalHost, self).__init__()

        # even if the config has an alias, we use localhost to make it clear that the host is localhost
        self.alias = config.get("alias", "localhost")
        self.address = "localhost"
        # we checkk local jobs more aggressively
        self.config = {"alias": self.alias, "status_check_interval": 2}
        self.config.update(config)

    def test_connection(self):
        return "OK"

    def target_exists(self, targets):
        return targets.target_exists()

    def target_signature(self, targets):
        return targets.target_signature()

    def send_to_host(self, items):
        return {x: x for x in items}

    def receive_from_host(self, items):
        return {x: x for x in items}

    def prepare_task(self, task_id):
        task_file = os.path.join(
            os.path.expanduser("~"), ".sos", "tasks", task_id + ".task"
        )
        # add server restriction on task file
        if not os.path.isfile(task_file):
            raise ValueError(f"Missing task definition {task_file}")

        tf = TaskFile(task_id)
        params, old_runtime = tf.get_params_and_runtime()
        # clear possible previous result
        task_vars = params.sos_dict
        runtime = {
            "_runtime": {
                x: task_vars["_runtime"][x]
                for x in (
                    "verbosity",
                    "sig_mode",
                    "run_mode",
                    "walltime",
                    "cores",
                    "mem",
                )
                if x in task_vars["_runtime"]
            }
        }
        runtime["_runtime"]["workdir"] = (
            task_vars["_runtime"]["workdir"]
            if "workdir" in task_vars["_runtime"]
            else os.getcwd()
        )

        if (
            "max_mem" in self.config
            or "max_cores" in self.config
            or "max_walltime" in self.config
        ):
            for key in ("max_mem", "max_cores", "max_walltime"):
                if key in self.config:
                    runtime["_runtime"][key] = (
                        format_HHMMSS(self.config[key])
                        if key == "max_walltime"
                        else self.config[key]
                    )

            if (
                self.config.get("max_mem", None) is not None
                and task_vars["_runtime"].get("mem", None) is not None
                and self.config["max_mem"] < task_vars["_runtime"]["mem"]
            ):
                env.logger.error(
                    f'Task {task_id} requested more mem ({task_vars["_runtime"]["mem"]}) than allowed max_mem ({self.config["max_mem"]})'
                )
                return False
            if (
                self.config.get("max_cores", None) is not None
                and task_vars["_runtime"].get("cores", None) is not None
                and self.config["max_cores"] < task_vars["_runtime"]["cores"]
            ):
                env.logger.error(
                    f'Task {task_id} requested more cores ({task_vars["_runtime"]["cores"]}) than allowed max_cores ({self.config["max_cores"]})'
                )
                return False
            if (
                self.config.get("max_walltime", None) is not None
                and task_vars["_runtime"].get("walltime", None) is not None
                and expand_time(self.config["max_walltime"])
                < expand_time(task_vars["_runtime"]["walltime"])
            ):
                env.logger.error(
                    f'Task {task_id} requested more walltime ({task_vars["_runtime"]["walltime"]}) than allowed max_walltime ({self.config["max_walltime"]})'
                )
                return False

        # if the task has been running remotely, we need to reset runtime for local execution
        if len(runtime) > 1 or runtime["_runtime"] or runtime != old_runtime:
            tf.runtime = runtime
        tf.status = "pending"
        self.send_job_file(task_file)
        return True

    def send_job_file(self, job_file, dir="tasks"):
        # on the same file system, no action is needed.
        dest_job_file = os.path.join(
            os.path.expanduser("~"), ".sos", dir, os.path.basename(job_file)
        )
        if job_file != dest_job_file:
            shutil.copyfile(job_file, dest_job_file)

    def check_output(self, cmd, under_workdir=False, **kwargs):
        # get the output of command
        if isinstance(cmd, list):
            cmd = subprocess.list2cmdline(cmd)
        try:
            cmd = cfg_interpolate(cmd)
            return subprocess.check_output(
                cmd, shell=isinstance(cmd, str), **kwargs
            ).decode()
        except Exception as e:
            env.logger.warning(f"Check output of {cmd} failed: {e}")
            raise

    def check_call(self, cmd, under_workdir=False, **kwargs):
        # get the output of command
        try:
            return subprocess.check_call(cmd, shell=isinstance(cmd, str), **kwargs)
        except Exception as e:
            env.logger.warning(f"Check output of {cmd} failed: {e}")
            raise

    def run_command(self, cmd, wait_for_task, realtime=False, **kwargs):
        # run command but does not wait for result.
        if realtime:
            from .utils import pexpect_run

            return pexpect_run(cmd)
        if isinstance(cmd, list):
            cmd = subprocess.list2cmdline(cmd)
        if wait_for_task or sys.platform == "win32":
            p = subprocess.Popen(cmd, shell=True, **kwargs)
            p.wait()
        else:
            p = DaemonizedProcess(cmd, **kwargs)
            p.start()
            p.join()

    def receive_result(self, task_id: str) -> Dict[str, Any]:
        tf = TaskFile(task_id)

        res = tf.result
        if not res or "ret_code" not in res:
            return {
                "ret_code": 1,
                "task": task_id,
                "exception": ValueError(f"No result is received for task {task_id}"),
            }

        try:
            if res["ret_code"] != 0 or env.verbosity >= 3:
                _show_err_and_out(task_id, res)
        except Exception as e:
            # if ret_code does not exist...
            return {"ret_code": 1, "output": {}, "exception": e}
        return res


class RemoteHost(object):
    """A remote host class that manages how to communicate with remote host"""

    def __init__(
        self,
        config: Dict[str, Union[str, int, List[str]]],
        test_connection: bool = True,
    ) -> None:
        self.config = config
        self.cm_opts = self._get_control_master_options()
        self.pem_opts = self._get_identify_file_options()
        self.alias = self.config["alias"]
        #
        self.address = self.config["address"]
        self.port = self.config.get("port", 22)
        self.pem_file = self.config.get("pem_file", None)
        self.shared_dirs = self._get_shared_dirs()
        self.path_map = self._get_path_map()
        # we already test connect of remote hosts
        if test_connection:
            test_res = self.test_connection()
            if test_res != "OK":
                raise RuntimeError(f"Failed to connect to {self.alias}: {test_res}")

    def target_exists(self, targets):
        try:
            msg = self.check_output(
                [
                    "sos",
                    "preview",
                    "--exists",
                    base64.b64encode(repr(targets).encode()).decode(),
                ],
                under_workdir=True,
            ).strip()
        except Exception as e:
            msg = f"error: {e}"
        if msg.startswith("error:"):
            env.logger.debug(msg)
            return True
        return msg == "yes"

    def target_signature(self, targets):
        try:
            msg = self.check_output(
                [
                    "sos",
                    "preview",
                    "--signature",
                    base64.b64encode(repr(targets).encode()).decode(),
                ],
                under_workdir=True,
            ).strip()
        except Exception as e:
            msg = f"error: {e}"
        if msg.startswith("error:"):
            env.logger.debug(msg)
            from .targets import textMD5
            return textMD5(targets.target_name())
        else:
            return msg

    def _get_shared_dirs(self) -> List[Any]:
        value = self.config.get("shared", [])
        if isinstance(value, str):
            return [value]
        if isinstance(value, Sequence):
            return value
        raise ValueError("Option shared can only be a string or a list of strings")

    def _get_path_map(self) -> Dict[str, str]:
        res: Dict = {}
        # if user-specified path_map, it overrides CONFIG
        path_map = self.config.get("path_map", [])
        #
        if not path_map:
            return res
        if isinstance(path_map, str):
            path_map = [path_map]
        if isinstance(path_map, Sequence):
            for v in path_map:
                if " -> " not in v:
                    raise ValueError(
                        f"Path map should be separated as from -> to, {v} specified"
                    )
                elif v.count(" -> ") > 1:
                    raise ValueError(
                        f"Path map should be separated as from -> to, {v} specified"
                    )
                res[v.split(" -> ")[0]] = v.split(" -> ")[1]
        elif isinstance(path_map, dict):
            for k, v in path_map.items():
                res[k] = v
        else:
            raise ValueError(
                f"Unacceptable path_mapue for configuration path_map: {path_map}"
            )
        return res

    def _get_control_master_options(self):
        master_dir = os.path.join(os.path.expanduser("~"), ".ssh", "controlmasters")
        if not os.path.isdir(master_dir):
            try:
                os.makedirs(master_dir, exist_ok=True)
            except Exception as e:
                env.logger.debug(
                    f"Failed to create ssh control master directory {master_dir}: {e}"
                )
                return ""
        return f"-o 'ControlMaster=auto' -o 'ControlPath={master_dir}/%r@%h:%p' -o 'ControlPersist=10m'"

    def _get_identify_file_options(self):
        if (
            "pem_file" in self.config
            and isinstance(self.config["pem_file"], str)
            and self.config["pem_file"].strip() != ""
        ):
            return f""" -i '{self.config["pem_file"]}' """
        return ""

    def _get_send_cmd(self, rename=False):
        if rename:
            return (
                "ssh "
                + self.cm_opts
                + self.pem_opts
                + """ -q {host} -p {port} "mkdir -p {dest:dpq}" && """
                + """rsync -a --no-g -e 'ssh """
                + self.cm_opts
                + self.pem_opts
                + """ -p {port}' {source:aep} "{host}:{dest:dep}" && """
                + """ssh """
                + self.cm_opts
                + self.pem_opts
                + """ -q {host} -p {port} "mv {dest:dep}/{source:b} {dest:ep}" """
            )
        return (
            "ssh "
            + self.cm_opts
            + self.pem_opts
            + """ -q {host} -p {port} "mkdir -p {dest:dpq}" """
            + """ && rsync -a --no-g -e "ssh -p {port} """
            + self.cm_opts
            + self.pem_opts
            + ''' " {source:aep} "{host}:{dest:dep}"'''
        )

    def _get_receive_cmd(self, rename=False):
        if rename:
            return (
                """rsync -a --no-g -e 'ssh """
                + self.cm_opts
                + self.pem_opts
                + """ -p {port}' {host}:{source:e} "{dest:adep}" && """
                + '''mv "{dest:adep}/{source:b}" "{dest:aep}"'''
            )
        return (
            """rsync -a --no-g -e 'ssh """
            + self.cm_opts
            + self.pem_opts
            + ''' -p {port}' {host}:{source:e} "{dest:adep}"'''
        )

    def _get_execute_cmd(self, under_workdir=True, use_heredoc=True) -> str:
        # #1396
        if "execute_cmd" in self.config:
            return self.config["execute_cmd"]
        if use_heredoc:
            return (
                "ssh "
                + self.cm_opts
                + self.pem_opts
                + """ -q {host} -p {port} <<'HEREDOC!!'\nbash --login -c '"""
                + (
                    " [ -d {workdir} ] || mkdir -p {workdir}; cd {workdir} && "
                    if under_workdir
                    else " "
                )
                + """ {cmd} '\nHEREDOC!!\n"""
            )
        else:
            return (
                "ssh "
                + self.cm_opts
                + self.pem_opts
                + """ -q {host} -p {port} "bash --login -c '"""
                + (
                    " [ -d {workdir} ] || mkdir -p {workdir}; cd {workdir} && "
                    if under_workdir
                    else " "
                )
                + """ {cmd}'" """
            )

    def _get_query_cmd(self):
        return self.config.get(
            "query_cmd",
            """ssh """
            + self.cm_opts
            + self.pem_opts
            + """ -q {host} -p {port} "bash --login -c '{self.config.get("sos", "sos")} status {task} -v 0'" """,
        )

    def is_shared(self, path):
        fullpath = os.path.abspath(os.path.expanduser(path))
        for sdir in self.shared_dirs:
            if fullpath.startswith(sdir):
                # issue 710, if a directory is both under path_map and shared, then it is not considered to be shared.
                if not any(fullpath.startswith(mdir) for mdir in self.path_map.keys()):
                    return True
        return False

    def _map_path(self, source):
        result = {}
        cwd = os.getcwd()
        if isinstance(source, str) and source.startswith("#"):
            result[path(source, host="localhost")] = path(source, host=self.alias)
        elif isinstance(source, (str, path)):
            dest = os.path.abspath(os.path.expanduser(source))
            # we use samefile to avoid problems with case-insensitive file system #522
            # we also use the "cwd" name to avoid wrong case for cwd. For example,
            # if the cwd = '/Users/user/Project'
            # then, dest = '/USERS/USER/PROJECT/a.txt'
            # would be converted to '/Users/user/Project/a.txt' before path mapping
            if os.path.exists(dest[: len(cwd)]) and os.path.samefile(
                dest[: len(cwd)], cwd
            ):
                dest = cwd + dest[len(cwd) :]
            matched = [
                k
                for k in self.path_map.keys()
                if os.path.exists(dest[: len(k)])
                and os.path.samefile(dest[: len(k)], k)
            ]
            if matched:
                # pick the longest key that matches
                k = max(matched, key=len)
                dest = self.path_map[k] + dest[len(k) :]
            else:
                env.logger.debug(
                    f"Path {source} is not under any specified paths of localhost and is mapped to {dest} on remote host."
                )
            result[source] = dest.replace("\\", "/")
        elif isinstance(source, (Sequence, sos_targets)):
            for src in source:
                result.update(self._map_path(src))
        else:
            env.logger.debug(f"Ignore unmappable source {source}")
            return {source: source}
        return result

    #
    # Interface functions
    #
    def _map_var(self, source):
        cwd = os.getcwd()
        if isinstance(source, path):
            source = str(source)
        if isinstance(source, str):
            if source.startswith("#"):
                return path(source, host=self.alias)
            dest = os.path.abspath(os.path.expanduser(source))
            # we use samefile to avoid problems with case-insensitive file system #522
            # we also use the "cwd" name to avoid wrong case for cwd. For example,
            # if the cwd = '/Users/user/Project'
            # then, dest = '/USERS/USER/PROJECT/a.txt'
            # would be converted to '/Users/user/Project/a.txt' before path mapping
            if os.path.exists(dest[: len(cwd)]) and os.path.samefile(
                dest[: len(cwd)], cwd
            ):
                dest = cwd + dest[len(cwd) :]
            matched = [
                k
                for k in self.path_map.keys()
                if os.path.exists(dest[: len(k)])
                and os.path.samefile(dest[: len(k)], k)
            ]
            if matched:
                # pick the longest key that matches
                k = max(matched, key=len)
                dest = self.path_map[k] + dest[len(k) :]
            else:
                env.logger.debug(
                    f"Path {source} is not under any specified paths of localhost and is mapped to {dest} on remote host."
                )
            return dest.replace("\\", "/")
        elif isinstance(source, (Sequence, set, sos_targets)):
            ret = [self._map_var(x) for x in source]
            return [x for x in ret if x is not None]
        else:
            env.logger.debug(f"Ignore unmappable source {source}")
            return source

    def test_connection(self):
        try:
            cmd = cfg_interpolate(
                "ssh {host} {pf_opt} -p {port} true",
                {
                    "host": self.address,
                    "port": self.port,
                    "pf_opt": f"-i '{self.pem_file}'" if self.pem_file else "",
                },
            )
            p = pexpect.spawn(cmd)
            # could be prompted for Password or password, so use assword
            while True:
                i = p.expect(
                    [
                        "(?i)are you sure you want to continue connecting",
                        "[pP]assword:",
                        pexpect.EOF,
                    ],
                    timeout=5,
                )
                if i == 0:
                    p.sendline("yes")
                elif i == 1:
                    p.close(force=True)
                    from .remote import stty_sane

                    stty_sane()
                    return f"ssh connection to {self.address} was prompted for password. Please set up public key authentication to the remote host before continue."
                elif i == 2:
                    p.close()
                    if p.exitstatus == 0:
                        return "OK"
                    elif p.before:
                        return p.before.decode()
                    else:
                        return f'Command "{cmd}" exits with code {p.exitstatus}'
        except pexpect.TIMEOUT:
            return f"ssh connection to {self.address} time out with prompt: {str(p.before)}"
        except Exception as e:
            return f"Failed to check remote connection {self.address}:{self.port}: {e}"
        return "OK"

    def _reverse_map_var(self, dest):
        if isinstance(dest, path):
            dest = str(dest)
        if isinstance(dest, str):
            matched = [
                l
                for l, r in self.path_map.items()
                if dest.startswith(r)
                and (
                    len(r) == len(dest)
                    or r.endswith("/")
                    or r.endswith("\\")
                    or dest[len(r)] in ("/", "\\")
                )
            ]
            if matched:
                # pick the longest key that matches
                k = max(matched, key=len)
                dest = k + dest[len(self.path_map[k]) :]
            else:
                env.logger.debug(
                    f"Path {dest} is not under any specified paths of localhost and is mapped to {dest} on local host."
                )
            return dest.replace("\\", "/")
        elif isinstance(dest, (Sequence, set, sos_targets)):
            ret = [self._reverse_map_var(x) for x in dest]
            return [x for x in ret if x is not None]
        else:
            env.logger.debug(f"Ignore unmappable source {dest}")
            return dest

    def _remote_abs(self, path):
        # return an absolute path relative to remote host
        path = str(path)
        if os.path.isabs(path):
            return path
        return os.path.join(self._map_var(os.getcwd()), path)

    def send_to_host(self, items):
        # we only copy files and directories, not other types of targets
        if isinstance(items, str):
            items = glob.glob(items)
        elif isinstance(items, path):
            items = [str(items)]
        elif isinstance(items, Sequence):
            ignored = [x for x in items if not isinstance(x, (str, path))]
            if ignored:
                env.logger.info(f"``Ignore`` {ignored}")
            items = sum(
                [
                    glob.glob(x) if isinstance(x, str) else [x]
                    for x in items
                    if x not in ignored
                ],
                [],
            )
        else:
            env.logger.warning(f"Unrecognized items to be sent to host: {items}")
            return {}

        from .utils import find_symbolic_links

        new_items = []
        for item in items:
            links = find_symbolic_links(item)
            for link, realpath in links.items():
                env.logger.info(f"Adding {realpath} for symbolic link {link}")
            new_items.extend(links.values())
        items.extend(new_items)

        sending = self._map_path(items)

        sent = {}
        for source in sorted(sending.keys()):
            dest = self._remote_abs(sending[source])
            if self.is_shared(source):
                if (
                    "TASK" in env.config["SOS_DEBUG"]
                    or "ALL" in env.config["SOS_DEBUG"]
                ):
                    env.log_to_file(
                        "TASK", f"Skip sending {source} on shared file system"
                    )
            else:
                if (
                    "TASK" in env.config["SOS_DEBUG"]
                    or "ALL" in env.config["SOS_DEBUG"]
                ):
                    env.log_to_file(
                        "TASK", f"Sending ``{source}`` to {self.alias}:{dest}"
                    )
                cmd = cfg_interpolate(
                    self._get_send_cmd(
                        rename=os.path.basename(source) != os.path.basename(dest)
                    ),
                    {
                        "source": sos_targets(str(source).rstrip("/")),
                        "dest": sos_targets(dest),
                        "host": self.address,
                        "port": self.port,
                    },
                )
                if (
                    "TASK" in env.config["SOS_DEBUG"]
                    or "ALL" in env.config["SOS_DEBUG"]
                ):
                    env.log_to_file("TASK", cmd)
                ret = subprocess.call(
                    cmd,
                    shell=True,
                    stderr=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                )
                if ret != 0:
                    raise RuntimeError(
                        f'Failed to copy {source} to {self.alias} using command "{cmd}". The remote host might be unavailable.'
                    )
            sent[source] = dest
        return sent

    def receive_from_host(self, items):
        if isinstance(items, str):
            items = [items]
        elif isinstance(items, path):
            items = [str(items)]
        elif isinstance(items, Sequence):
            ignored = [x for x in items if not isinstance(x, (str, path))]
            if ignored:
                env.logger.info(f"``Ignore`` {ignored}")
            items = [x for x in items if isinstance(x, (str, path))]
        else:
            env.logger.warning(f"Unrecognized items to be retrieved from host: {items}")
            return {}

        # y could be path
        receiving = {
            self._remote_abs(y): str(x) for x, y in self._map_path(items).items()
        }
        #
        received = {}
        for source in sorted(receiving.keys()):
            dest = receiving[source]
            dest_dir = os.path.dirname(dest)
            if dest_dir and not os.path.isdir(dest_dir):
                try:
                    os.makedirs(dest_dir)
                except Exception as e:
                    env.logger.error(
                        f"Failed to create destination directory {dest_dir}: {e}"
                    )
            if self.is_shared(dest) and os.path.basename(source) == os.path.basename(
                dest
            ):
                env.logger.debug(f"Skip retrieving ``{dest}`` from shared file system")
                received[dest] = source
            else:
                cmd = cfg_interpolate(
                    self._get_receive_cmd(
                        rename=os.path.basename(source) != os.path.basename(dest)
                    ),
                    {
                        "source": sos_targets(str(source).rstrip("/")),
                        "dest": sos_targets(dest),
                        "host": self.address,
                        "port": self.port,
                    },
                )
                if (
                    "TASK" in env.config["SOS_DEBUG"]
                    or "ALL" in env.config["SOS_DEBUG"]
                ):
                    env.log_to_file("TASK", cmd)
                try:
                    ret = subprocess.call(
                        cmd,
                        shell=True,
                        stderr=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                    )
                    if ret != 0:
                        raise RuntimeError(f"command return {ret}")
                    received[dest] = source
                except Exception as e:
                    raise RuntimeError(
                        f'Failed to copy {source} from {self.alias} using command "{cmd}": {e}'
                    )
        return received

    #
    # Interface
    #
    def prepare_task(self, task_id):
        try:
            self._prepare_task(task_id)
            return True
        except Exception as e:
            env.logger.error(str(e))
            return False

    def _prepare_task(self, task_id):
        task_file = os.path.join(
            os.path.expanduser("~"), ".sos", "tasks", task_id + ".task"
        )
        if not os.path.isfile(task_file):
            raise ValueError(f"Missing task definition {task_file}")
        tf = TaskFile(task_id)
        params, old_runtime = tf.get_params_and_runtime()
        task_vars = params.sos_dict
        runtime = {
            "_runtime": {
                x: task_vars["_runtime"][x]
                for x in (
                    "verbosity",
                    "sig_mode",
                    "run_mode",
                    "walltime",
                    "cores",
                    "mem",
                )
                if x in task_vars["_runtime"]
            },
            task_id: {},
        }

        if (
            self.config.get("max_mem", None) is not None
            and task_vars["_runtime"].get("mem", None) is not None
            and self.config["max_mem"] < task_vars["_runtime"]["mem"]
        ):
            raise ValueError(
                f'Task {task_id} requested more mem ({task_vars["_runtime"]["mem"]}) than allowed max_mem ({self.config["max_mem"]})'
            )
        if (
            self.config.get("max_cores", None) is not None
            and task_vars["_runtime"].get("cores", None) is not None
            and self.config["max_cores"] < task_vars["_runtime"]["cores"]
        ):
            raise ValueError(
                f"Task {task_id} requested more cores ({task_vars['_runtime']['cores']}) than allowed max_cores ({self.config['max_cores']})"
            )
        if (
            self.config.get("max_walltime", None) is not None
            and task_vars["_runtime"].get("walltime", None) is not None
            and expand_time(self.config["max_walltime"])
            < expand_time(task_vars["_runtime"]["walltime"])
        ):
            raise ValueError(
                f'Task {task_id} requested more walltime ({task_vars["_runtime"]["walltime"]}) than allowed max_walltime ({self.config["max_walltime"]})'
            )

        if task_vars["_input"] and not isinstance(task_vars["_input"], Undetermined):
            sent = self.send_to_host(task_vars["_input"])
            if sent:
                env.logger.info(
                    f"{task_id} ``sent`` {short_repr(sent.keys())} to {self.alias}"
                )
        if task_vars["_depends"] and not isinstance(
            task_vars["_depends"], Undetermined
        ):
            sent = self.send_to_host(task_vars["_depends"])
            if sent:
                env.logger.info(
                    f"{task_id} ``sent`` {short_repr(sent.keys())} to {self.alias}"
                )
        if "to_host" in task_vars["_runtime"]:
            if not isinstance(task_vars["_runtime"]["to_host"], (str, Sequence)) or (
                isinstance(task_vars["_runtime"]["to_host"], Sequence)
                and not all(
                    isinstance(x, str) for x in task_vars["_runtime"]["to_host"]
                )
            ):
                raise ValueError(
                    f'Parameter to_host accepts a list of paths (strings). {task_vars["_runtime"]["to_host"]} provided'
                )

            sent = self.send_to_host(task_vars["_runtime"]["to_host"])
            if sent:
                env.logger.info(
                    f"{task_id} ``sent`` {short_repr(sent.keys())} to {self.alias}"
                )

        # map variables
        runtime["_runtime"]["workdir"] = task_vars["_runtime"]["workdir"] if "workdir" in task_vars["_runtime"] else path.cwd().shrink()

        if runtime["_runtime"]["workdir"].startswith('#'):
            try:
                path(runtime["_runtime"]["workdir"], host=self.alias)
            except Exception as e:
                raise ValueError(f'Working directory {runtime["_runtime"]["workdir"]} does not exist on remote host {self.alias}')
        elif path(runtime["_runtime"]["workdir"]).is_absolute():
            env.logger.warning(f'Using absolute path as workdir is not portable.')

        env.log_to_file('TASK', f'Set workdir to {runtime["_runtime"]["workdir"]}')
        mapped_vars = {"_input", "_output", "_depends", "input", "output", "depends"}

        # for var in mapped_vars:
        #     if var not in task_vars:
        #         # input, output, depends might not exist
        #         continue
        #     if not task_vars[var]:
        #         continue
        #     elif isinstance(task_vars[var], str):
        #         runtime[task_id][var] = self._map_var(task_vars[var])
        #         env.log_to_file(
        #             "TASK", f"On {self.alias}: ``{var}`` = {short_repr(task_vars[var])}"
        #         )
        #     elif isinstance(task_vars[var], (Sequence, set)):
        #         runtime[task_id][var] = type(task_vars[var])(
        #             self._map_var(task_vars[var])
        #         )
        #         env.log_to_file(
        #             "TASK", f"On {self.alias}: ``{var}`` = {short_repr(task_vars[var])}"
        #         )
        #     else:
        #         env.logger.warning(
        #             f"Failed to map {var} of type {task_vars[var].__class__.__name__}"
        #         )

        # master task??
        # if hasattr(params, "task_stack"):
        #     for tid, tdef in params.task_stack:
        #         runtime[tid] = {}
        #         for var in mapped_vars:
        #             if var not in tdef.sos_dict:
        #                 # input, output, depends might not exist
        #                 continue
        #             if not tdef.sos_dict[var]:
        #                 continue
        #             elif isinstance(tdef.sos_dict[var], str):
        #                 runtime[tid][var] = self._map_var(tdef.sos_dict[var])
        #             elif isinstance(tdef.sos_dict[var], (Sequence, set)):
        #                 runtime[tid][var] = type(tdef.sos_dict[var])(
        #                     self._map_var(tdef.sos_dict[var])
        #                 )
        #             else:
        #                 env.logger.warning(
        #                     f"Failed to map {var} of type {tdef.sos_dict[var].__class__.__name__}"
        #                 )

        # server restrictions #488
        for key in ("max_mem", "max_cores", "max_walltime"):
            if key in self.config:
                runtime["_runtime"][key] = (
                    format_HHMMSS(self.config[key])
                    if key == "max_walltime"
                    else self.config[key]
                )
        runtime['_runtime']['localhost'] = get_config(['hosts', self.alias])

        # only update task file if there are runtime information
        if len(runtime) > 1 or runtime["_runtime"] or runtime != old_runtime:
            tf.runtime = runtime

        tf.status = "pending"
        self.send_job_file(task_file)

    def send_job_file(self, job_file, dir="tasks"):
        send_cmd = cfg_interpolate(
            f"ssh {self.cm_opts + self.pem_opts}"
            f' -q {{address}} -p {{port}} "[ -d ~/.sos/{dir} ] || mkdir -p ~/.sos/{dir}" && '
            f' rsync --ignore-existing -a --no-g -e "ssh {self.cm_opts + self.pem_opts}'
            f' -q -p {{port}}" {{job_file:ap}} {{address}}:.sos/{dir}/',
            {
                "job_file": sos_targets(job_file),
                "address": self.address,
                "port": self.port,
            },
        )
        # use scp for this simple case
        try:
            subprocess.check_call(send_cmd, shell=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to copy job {job_file} to {self.alias} using command {send_cmd}: {e}"
            )

    def check_output(self, cmd: object, under_workdir=False, **kwargs) -> object:
        if isinstance(cmd, list):
            cmd = subprocess.list2cmdline(cmd)
        try:
            cmd = cfg_interpolate(
                self._get_execute_cmd(
                    under_workdir=under_workdir, use_heredoc="." in cmd
                ),
                {
                    "host": self.address,
                    "port": self.port,
                    "cmd": cmd.replace("'", r"'\''"),
                    "workdir": self._map_var(os.getcwd()),
                },
            )
        except Exception as e:
            raise ValueError(
                f'Failed to run command {cmd}: {e} ({env.sos_dict["CONFIG"]})'
            )
        if "TASK" in env.config["SOS_DEBUG"] or "ALL" in env.config["SOS_DEBUG"]:
            env.log_to_file("TASK", f"Executing command ``{cmd}``")
        try:
            return subprocess.check_output(cmd, shell=True, **kwargs).decode()
        except Exception as e:
            env.logger.debug(f"Check output of {cmd} failed: {e}")
            raise

    def check_call(self, cmd, under_workdir=False, **kwargs):
        if isinstance(cmd, list):
            cmd = subprocess.list2cmdline(cmd)
        try:
            cmd = cfg_interpolate(
                self._get_execute_cmd(
                    under_workdir=under_workdir, use_heredoc="." in cmd
                ),
                {
                    "host": self.address,
                    "port": self.port,
                    "cmd": cmd.replace("'", r"'\''"),
                    "workdir": self._map_var(os.getcwd()),
                },
            )
        except Exception as e:
            raise ValueError(f"Failed to run command {cmd}: {e}")
        if "TASK" in env.config["SOS_DEBUG"] or "ALL" in env.config["SOS_DEBUG"]:
            env.log_to_file("TASK", f"Executing command ``{cmd}``")
        try:
            return subprocess.check_call(cmd, shell=True, **kwargs)
        except Exception as e:
            env.logger.debug(f"Check output of {cmd} failed: {e}")
            raise

    def run_command(self, cmd, wait_for_task, realtime=False, **kwargs):
        if isinstance(cmd, list):
            cmd = subprocess.list2cmdline(cmd)
        try:
            cmd = cfg_interpolate(
                self._get_execute_cmd(under_workdir=False, use_heredoc="." in cmd),
                {
                    "host": self.address,
                    "port": self.port,
                    "cmd": cmd.replace("'", r"'\''"),
                    "workdir": self._map_var(os.getcwd()),
                },
            )
        except Exception as e:
            raise ValueError(f"Failed to run command {cmd}: {e}")
        if "TASK" in env.config["SOS_DEBUG"] or "ALL" in env.config["SOS_DEBUG"]:
            env.log_to_file("TASK", f"Executing command ``{cmd}``")
        if realtime:
            from .utils import pexpect_run

            return pexpect_run(cmd)
        elif wait_for_task or sys.platform == "win32":
            # keep proc persistent to avoid a subprocess is still running warning.
            p = subprocess.Popen(cmd, shell=True, **kwargs)
            p.wait()
        else:
            p = DaemonizedProcess(cmd, **kwargs)
            p.start()
            p.join()

    def receive_result(self, task_id: str) -> Dict[str, int]:
        # for filetype in ('res', 'status', 'out', 'err'):
        sys_task_dir = os.path.join(os.path.expanduser("~"), ".sos", "tasks")
        # use -p to preserve modification times so that we can keep the job status locally.
        receive_cmd = cfg_interpolate(
            "scp -P {port} {pem_opts} -p -q {address}:.sos/tasks/{task}.* {sys_task_dir}",
            {
                "port": self.port,
                "address": self.address,
                "task": task_id,
                "sys_task_dir": sys_task_dir,
                "pem_opts": self.pem_opts,
            },
        )
        # it is possible that local files are readonly (e.g. a pluse file) so we first need to
        # make sure the files are readable and remove them. Also, we do not want any file that is
        # obsolete to appear as new after copying
        for lfile in glob.glob(
            os.path.join(os.path.expanduser("~"), ".sos", "tasks", task_id + ".*")
        ):
            if not os.access(lfile, os.W_OK):
                os.chmod(lfile, stat.S_IREAD | stat.S_IWRITE)
            # os.remove(lfile)
        if "TASK" in env.config["SOS_DEBUG"] or "ALL" in env.config["SOS_DEBUG"]:
            env.log_to_file("TASK", receive_cmd)
        ret = subprocess.call(
            receive_cmd,
            shell=True,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        if ret != 0:
            # this time try to get error message
            ret = subprocess.call(receive_cmd, shell=True)
            if ret != 0:
                raise RuntimeError(
                    "Failed to retrieve result of job {} from {} with cmd\n{}".format(
                        task_id, self.alias, receive_cmd
                    )
                )

        tf = TaskFile(task_id)
        params = tf.params
        res = tf.result

        if not res:
            env.logger.debug(f"Result for {task_id} is not received (no result)")
            return {
                "ret_code": 1,
                "task": task_id,
                "exception": RuntimeError(f"Task {task_id} failed or aborted"),
                "output": sos_targets(),
            }

        if ("ret_code" in res and res["ret_code"] != 0) or (
            "succ" in res and res["succ"] != 0
        ):
            _show_err_and_out(task_id, res)
            env.logger.info(f"Ignore remote results for failed job {task_id}")
            return res

        if env.verbosity >= 3:
            _show_err_and_out(task_id, res)
        # do we need to copy files? We need to consult original task file
        # not the converted one
        job_dict = params.sos_dict
        if (
            "_output" in job_dict
            and job_dict["_output"]
            and not isinstance(job_dict["_output"], Undetermined)
            and env.config["run_mode"] != "dryrun"
        ):
            received = self.receive_from_host(
                [x for x in job_dict["_output"] if isinstance(x, (str, path))]
            )
            if received:
                env.logger.info(
                    f"{task_id} ``received`` {short_repr(received.keys())} from {self.alias}"
                )
        if "from_host" in job_dict["_runtime"] and env.config["run_mode"] != "dryrun":
            if isinstance(job_dict["_runtime"]["from_host"], (Sequence, str)):
                received = self.receive_from_host(job_dict["_runtime"]["from_host"])
                if received:
                    env.logger.info(
                        f"{task_id} ``received`` {short_repr(received.keys())} from {self.alias}"
                    )
            else:
                env.logger.warning(
                    f"Expecting a string or list of string from from_host: {job_dict['_runtime']['from_host']} received"
                )
        # we need to translate result from remote path to local
        if "output" in res:
            if "_output" not in job_dict:
                # this should exist, but let us check it for robustness
                env.logger.warning("Missing _output in task dict")
                res["output"] = sos_targets()
            elif job_dict["_output"].undetermined():
                res["output"] = sos_targets(self._reverse_map_var(res["output"]))
            else:
                res["output"] = job_dict["_output"]
        if "subtasks" in res:
            for tid, subparams in params.task_stack:
                if tid in res["subtasks"] and "output" in res["subtasks"][tid]:
                    if "_output" not in subparams.sos_dict:
                        # this should not happen
                        env.logger.warning("Missing _output in subparams")
                        res["subtasks"][tid]["output"] = sos_targets()
                    elif subparams.sos_dict["_output"].undetermined():
                        res["subtasks"][tid]["output"] = sos_targets(
                            self._reverse_map_var(res["subtasks"][tid]["output"])
                        )
                    else:
                        res["subtasks"][tid]["output"] = subparams.sos_dict["_output"]
        return res


#
# host instances are shared by all tasks so there should be only one
# instance for each host.
#


class Host:
    host_instances: Dict = {}

    def __init__(
        self,
        alias: Optional[str] = "",
        start_engine: bool = True,
        test_connection: bool = True,
    ) -> None:
        # a host started from Jupyter notebook might not have proper stdout
        # (a StringIO) and cannot be used for DaemonizedFork).
        self._get_config(alias)
        self._get_host_agent(start_engine, test_connection)

    # for test purpose
    @classmethod
    def reset(cls) -> None:
        for host in cls.host_instances.values():
            # perhaps the engine failed to start
            if hasattr(host, "_task_engine"):
                del host._task_engine
        cls.host_instances = {}

    def _get_local_host(self) -> str:
        if "CONFIG" not in env.sos_dict or "hosts" not in env.sos_dict["CONFIG"]:
            from .utils import load_config_files

            load_config_files()
        # look for an entry with gethost
        if "hosts" not in env.sos_dict["CONFIG"]:
            env.sos_dict["CONFIG"]["hosts"] = {
                "localhost": {"address": "localhost", "alias": "localhost"}
            }
            return "localhost"
        # try host name
        hostname = socket.gethostname().lower()
        for host, host_info in env.sos_dict["CONFIG"]["hosts"].items():
            # find by key hostname
            if "hostname" in host_info and host_info["hostname"].lower() == hostname:
                return host
        for host, host_info in env.sos_dict["CONFIG"]["hosts"].items():
            # find by key hostname
            if "hostname" in host_info:
                hn = get_config("hosts", host, "hostname", expected_type=str).lower()
                if hn.split(".")[0] == hostname or hn == hostname.split(".")[0]:
                    return host
        for host, host_info in env.sos_dict["CONFIG"]["hosts"].items():
            # find by alias
            if host.lower() == hostname:
                return host
        for host, host_info in env.sos_dict["CONFIG"]["hosts"].items():
            # find by address
            if "address" in host_info:
                addr = get_config("hosts", host, "address", expected_type=str)
                if (
                    addr.split("@")[-1].lower() == hostname
                    or addr.split(".", 1)[0].split("@")[-1].lower() == hostname
                ):
                    return host
        # try IP Address
        hostname = socket.gethostname()
        ips = socket.gethostbyname_ex(hostname)[2]
        ips = [ip for ip in ips if not ip.startswith("127.")]
        # if the IP matches any address?
        for host, host_info in env.sos_dict["CONFIG"]["hosts"].items():
            # find by key hostname
            if "address" in host_info:
                addr = get_config("hosts", host, "address", expected_type=str)
                if any(ip == addr.split("@")[-1] for ip in ips):
                    return host
        # now check if a key localhost is defined
        if "localhost" in env.sos_dict["CONFIG"]:
            if (
                env.sos_dict["CONFIG"]["localhost"]
                not in env.sos_dict["CONFIG"]["hosts"]
            ):
                raise ValueError(
                    f"Undefined localhost {env.sos_dict['CONFIG']['localhost']}"
                )
            return env.sos_dict["CONFIG"]["localhost"]
        else:
            env.sos_dict["CONFIG"]["localhost"] = "localhost"
            return "localhost"

    def _get_remote_host(self, alias: Optional[str]) -> str:
        # get a remote host specified by Alias
        if "CONFIG" not in env.sos_dict or "hosts" not in env.sos_dict["CONFIG"]:
            from .utils import load_config_files

            load_config_files()
        if not alias or alias == "localhost":
            return self._get_local_host()
        if not isinstance(alias, str):
            raise ValueError(f"A string is expected for host {alias}")
        if "hosts" not in env.sos_dict["CONFIG"]:
            env.sos_dict["CONFIG"]["hosts"] = {}
        if alias in env.sos_dict["CONFIG"]["hosts"]:
            return alias
        # assuming the host name is a name or IP address
        env.logger.debug(
            f"Assuming {alias} to be a hostname or IP address not defined in hosts file"
        )
        env.sos_dict["CONFIG"]["hosts"][alias] = {
            "address": alias,
            "alias": alias,
        }
        return alias

    def _get_config(self, alias: Optional[str]) -> None:
        LOCAL = self._get_local_host()
        REMOTE = self._get_remote_host(alias)
        self.alias = REMOTE

        # now we need to find definition for local and remote host
        if LOCAL == "localhost" and REMOTE == "localhost":
            self.config = {
                "address": "localhost",
                "alias": "localhost",
            }
        elif "hosts" in env.sos_dict["CONFIG"]:
            if LOCAL not in env.sos_dict["CONFIG"]["hosts"]:
                raise ValueError(f"Undefined local host {LOCAL}")
            if REMOTE not in env.sos_dict["CONFIG"]["hosts"]:
                raise ValueError(f"Undefined remote host {REMOTE}")

            # now we have definition for local and remote hosts but we only
            # exapnd address, which we have to know right now
            cfg = env.sos_dict["CONFIG"]["hosts"]
            self.config = get_config(
                "hosts",
                self.alias,
                excluded_keys=("paths", "shared"),
                expand_keys=("address", "port", "pem_file"),
                expected_type=dict,
            )
            # if local and remote hosts are the same
            if (
                LOCAL == REMOTE
                or "address" not in env.sos_dict["CONFIG"]["hosts"][REMOTE]
                or (
                    "address" in env.sos_dict["CONFIG"]["hosts"][REMOTE]
                    and env.sos_dict["CONFIG"]["hosts"][REMOTE]["address"]
                    == "localhost"
                )
            ):
                # there would be no path map
                self.config["path_map"] = []
                self.config["shared"] = ["/"]
                # override address setting to use localhost
                self.config["address"] = "localhost"
            else:
                self.config["path_map"] = []

                def normalize_value(x):
                    x = cfg_interpolate(x)
                    return x if x.endswith(os.sep) else (x + os.sep)

                if "shared" in cfg[LOCAL] and "shared" in cfg[REMOTE]:
                    common = set(cfg[LOCAL]["shared"].keys()) & set(
                        cfg[REMOTE]["shared"].keys()
                    )
                    if common:
                        lcl_shrd = get_config(
                            "hosts", LOCAL, "shared", expected_type=dict
                        )
                        rmt_shrd = get_config(
                            "hosts", REMOTE, "shared", expected_type=dict
                        )
                        self.config["shared"] = [
                            normalize_value(lcl_shrd[x]) for x in common
                        ]
                        self.config["path_map"] = [
                            f"{normalize_value(lcl_shrd[x])} -> {normalize_value(rmt_shrd[x])}"
                            for x in common
                        ]
                # if paths are defined for both local and remote host, define path_map
                if ("paths" in cfg[LOCAL] and cfg[LOCAL]["paths"]) and (
                    "paths" in cfg[REMOTE] and cfg[REMOTE]["paths"]
                ):
                    if any(
                        k not in cfg[REMOTE]["paths"]
                        for k in cfg[LOCAL]["paths"].keys()
                    ):
                        env.logger.debug(
                            f'One or more local paths {", ".join(cfg[LOCAL]["paths"].keys())} cannot be mapped to remote host {REMOTE} with paths {",".join(cfg[REMOTE]["paths"].keys())}'
                        )
                    #
                    lpaths = get_config("hosts", LOCAL, "paths", expected_type=dict)
                    rpaths = get_config("hosts", REMOTE, "paths", expected_type=dict)
                    self.config["path_map"].extend(
                        [
                            f"{normalize_value(lpaths[x])} -> {normalize_value(rpaths[x])}"
                            for x in lpaths.keys()
                            if x in rpaths
                        ]
                    )
                if "pem_file" in cfg[LOCAL]:
                    if isinstance(cfg[LOCAL]["pem_file"], dict):
                        if REMOTE in cfg[LOCAL]["pem_file"]:
                            self.config["pem_file"] = get_config(
                                "hosts", LOCAL, "pem_file", REMOTE, expected_type=str
                            )
                    elif isinstance(cfg[LOCAL]["pem_file"], str):
                        self.config["pem_file"] = get_config(
                            "hosts", LOCAL, "pem_file", expected_type=str
                        )
                    else:
                        raise ValueError(
                            f"Option pem_file should be a string or dictionary, {cfg[LOCAL]['pem_file']} provided."
                        )
        elif LOCAL == REMOTE:
            # now we have checked local and remote are not defined, but they are the same, so
            # it is safe to assume that they are both local hosts
            self.config = {
                "address": "localhost",
                "alias": LOCAL,
            }
        else:
            raise ValueError(f"Undefined local and remote hosts {LOCAL} and {REMOTE}.")
        #
        self.config["alias"] = self.alias
        self.description = self.config.get("description", "")

        # standardize parameters max_walltime, max_cores, and max_mem for the host
        if "max_walltime" in self.config:
            self.config["max_walltime"] = format_HHMMSS(self.config["max_walltime"])
        if "max_cores" in self.config:
            if not isinstance(self.config["max_cores"], int):
                raise ValueError("An integer is expected for max_cores")
        if "max_mem" in self.config:
            self.config["max_mem"] = expand_size(self.config["max_mem"])

    def _get_task_and_workflow_engine(self):
        if self._engine_type == "process":
            task_engine = BackgroundProcess_TaskEngine(
                self.host_instances[self.alias]
            )
            workflow_engine = BackgroundProcess_WorkflowEngine(
                self.host_instances[self.alias]
            )
        else:
            task_engine = None
            workflow_engine = None

            for entrypoint in pkg_resources.iter_entry_points(
                group="sos_taskengines"
            ):
                try:
                    if entrypoint.name == self._engine_type:
                        task_engine = entrypoint.load()(
                            self.host_instances[self.alias]
                        )
                        break
                except Exception as e:
                    env.logger.debug(
                        f"Failed to load task engine {self._engine_type}: {e}"
                    )

            for entrypoint in pkg_resources.iter_entry_points(
                group="sos_workflowengines"
            ):
                try:
                    if entrypoint.name == self._engine_type:
                        workflow_engine = entrypoint.load()(
                            self.host_instances[self.alias]
                        )
                        break
                except Exception as e:
                    env.logger.debug(
                        f"Failed to load workflow engine {self._engine_type}: {e}"
                    )

            if task_engine is None and workflow_engine is None:
                raise ValueError(
                    f'Failed to load task engine of type "{self._engine_type}". Please check the engine name or install relevant module.'
                )
        return task_engine, workflow_engine

    def _get_host_agent(self, start_engine: bool, test_connection: bool) -> None:
        if "queue_type" not in self.config:
            self._engine_type = "process"
        else:
            self._engine_type = self.config["queue_type"].strip()
        # if there is no engine, or if the engine was stopped
        if self.alias not in self.host_instances or (
            hasattr(self.host_instances[self.alias], '_task_engine') and
            self.host_instances[self.alias]._task_engine._is_stopped):
            if self.config["address"] == "localhost":
                self.host_instances[self.alias] = LocalHost(
                    self.config, test_connection=test_connection
                )
            else:
                self.host_instances[self.alias] = RemoteHost(
                    self.config, test_connection=test_connection
                )

            task_engine, workflow_engine = self._get_task_and_workflow_engine()
            self.host_instances[self.alias]._task_engine = task_engine
            self.host_instances[self.alias]._workflow_engine = workflow_engine

            # # the task engine is a thread and will run continously
            # if start_engine and task_engine is not None:
            #     task_engine.start()

        self._host_agent = self.host_instances[self.alias]
        # for convenience
        if hasattr(self._host_agent, "_task_engine"):
            self._task_engine = self._host_agent._task_engine
        if hasattr(self._host_agent, "_workflow_engine"):
            self._workflow_engine = self._host_agent._workflow_engine
        # it is possible that Host() is initialized before with start_engine=False
        # and called again to start engine
        if (
            start_engine
            and self._task_engine is not None
            and not self._task_engine.is_alive()
        ):
            self._task_engine.start()


    # public interface
    #
    # based on Host definition
    #
    def send_to_host(self, items):
        return self._host_agent.send_to_host(items)

    def receive_from_host(self, items):
        return self._host_agent.receive_from_host(items)

    def map_var(self, rvars):
        return self._host_agent._map_var(rvars)

    def target_exists(self, targets):
        return self._host_agent.target_exists(targets)

    def target_signature(self, targets):
        return self._host_agent.target_signature(targets)

    def submit_task(self, task_id: str) -> str:
        if not self._task_engine:
            raise RuntimeError(
                f"No task engine or invalid engine definition defined for host {self.alias}"
            )
        return self._task_engine.submit_task(task_id)

    def check_status(self, tasks: List[str]) -> List[str]:
        # find the task engine
        return [self._task_engine.check_task_status(task) for task in tasks]

    def retrieve_results(self, tasks: List[str]):
        return self._task_engine.get_results(tasks)

    def execute_workflow(self, script, cmd, **template_args):
        if not self._workflow_engine:
            raise RuntimeError(
                f"No workflow engine or invalid engine definition defined for host {self.alias}"
            )
        return self._workflow_engine.execute_workflow(script, cmd, **template_args)
