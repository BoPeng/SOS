#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import copy
import gzip
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import tempfile
import textwrap
import urllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Sequence
from functools import wraps

from tqdm import tqdm as ProgressBar
from concurrent.futures import ProcessPoolExecutor

from .eval import interpolate
from .parser import SoS_Script
from .syntax import SOS_ACTION_OPTIONS
from .targets import executable, file_target, path, paths, sos_targets
from .utils import (
    textMD5,
    fileMD5,
    StopInputGroup,
    TerminateExecution,
    TimeoutInterProcessLock,
    env,
    get_traceback,
    short_repr,
    transcribe,
    load_config_files,
)
from .controller import send_message_to_controller
from .messages import encode_msg, decode_msg

from typing import Any, Callable, Dict, List, Tuple, Union

__all__ = [
    "SoS_Action",
    "script",
    "sos_run",
    "fail_if",
    "warn_if",
    "stop_if",
    "download",
    "run",
    "perl",
    "report",
    "pandoc",
]


def get_actions() -> List[Any]:
    # get the name of all actions, which are identified by an attribute
    # run_mode of the function
    return [k for k, v in globals().items() if hasattr(v, "run_mode")]


#
# A decoration function that allows SoS to replace all SoS actions
# with a null action. Option run_mode is deprecated and might be
# removed later on.
#


def SoS_Action(
    run_mode: Union[str, List[str]] = "deprecated",
    acceptable_args: Union[Tuple[str], List[str]] = ("*",),
    default_args: Dict[str, Dict[str, str]] = {},
) -> Callable:
    def runtime_decorator(func):
        @wraps(func)
        def action_wrapper(*args, **kwargs):
            # if container in args, a large number of docker-specific
            # args would be allowed.
            for k in default_args:
                if k in default_args and k not in kwargs:
                    kwargs[k] = default_args[k]
            if "*" not in acceptable_args and all(
                x not in kwargs
                for x in ("docker_image", "container", "template", "template_name")
            ):
                for key in kwargs.keys():
                    if key not in acceptable_args and key not in SOS_ACTION_OPTIONS:
                        raise ValueError(
                            f'Unrecognized option "{key}" for action {func}'
                        )
            # docker files will be downloaded in run or prepare mode
            # this option is independent of container...
            if "docker_file" in kwargs and env.config["run_mode"] in [
                "run",
                "interactive",
            ]:
                from .docker.client import SoS_DockerClient

                docker = SoS_DockerClient()
                docker.load_image(kwargs["docker_file"])
            # handle image
            if "docker_image" in kwargs:
                if "container" in kwargs and kwargs["container"]:
                    raise ValueError(
                        "Option docker_image is deprecated and should not be specified with option container"
                    )
                kwargs["container"] = "docker://" + kwargs["container"]
            if "container" in kwargs and kwargs["container"]:
                if not isinstance(kwargs["container"], str):
                    raise ValueError(
                        f'A string in the format of "scheme://tag" is expected for option container, {kwargs["container"]} provided'
                    )
                engine = (
                    kwargs["engine"]
                    if "engine" in kwargs and kwargs["engine"]
                    else None
                )
                if "://" in kwargs["container"]:
                    cty, cname = kwargs["container"].split("://", 1)
                elif kwargs["container"].endswith(".simg") or kwargs[
                    "container"
                ].endswith(".sif"):
                    engine = "singularity"
                    cty = "file"
                    cname = kwargs["container"]
                else:
                    cty = None
                    cname = kwargs["container"]
                # now let us figure out image and engine
                # if engine is specified
                if engine == "docker":
                    if cty is not None and cty != "docker":
                        raise ValueError(
                            f"docker engine only allows docker container {cty} specified"
                        )
                elif engine == "singularity":
                    if cty is not None and cty not in (
                        "docker",
                        "file",
                        "library",
                        "shub",
                    ):
                        raise ValueError(
                            f"singularity engine only allows docker, file, library, and shub container {cty} specified"
                        )
                elif engine is not None and engine != "local":
                    raise ValueError(
                        f"Only docker and singularity container engines are supported: {engine} specified"
                    )
                else:
                    # engine is none, need to be refered
                    if cty == "docker":
                        engine = "docker"
                    elif cty in ("file", "shub", "library"):
                        engine = "singularity"
                    elif cty == "local":
                        engine = "local"
                    else:
                        engine = "docker"
                #
                # handle different container type
                if engine == "docker":
                    from .docker.client import SoS_DockerClient

                    docker = SoS_DockerClient()
                    docker.pull(cname)
                    kwargs["engine"] = "docker"
                    kwargs["container"] = cname
                elif engine == "singularity":
                    kwargs["engine"] = "singularity"
                    from .singularity.client import SoS_SingularityClient

                    singularity = SoS_SingularityClient()
                    singularity.pull(kwargs["container"])
                else:
                    # if local or none, reset container
                    kwargs["engine"] = None
                    kwargs["container"] = None
            if "active" in kwargs:
                if kwargs["active"] is False:
                    return None
                elif kwargs["active"] is True:
                    pass
                elif isinstance(kwargs["active"], int):
                    if (
                        kwargs["active"] >= 0
                        and env.sos_dict["_index"] != kwargs["active"]
                    ):
                        return None
                    if (
                        kwargs["active"] < 0
                        and env.sos_dict["_index"]
                        != kwargs["active"] + env.sos_dict["__num_groups__"]
                    ):
                        return None
                elif isinstance(kwargs["active"], Sequence):
                    allowed_index = list(
                        [
                            x if x >= 0 else env.sos_dict["__num_groups__"] + x
                            for x in kwargs["active"]
                        ]
                    )
                    if env.sos_dict["_index"] not in allowed_index:
                        return None
                elif isinstance(kwargs["active"], slice):
                    allowed_index = list(range(env.sos_dict["__num_groups__"]))[
                        kwargs["active"]
                    ]
                    if env.sos_dict["_index"] not in allowed_index:
                        return None
                else:
                    raise RuntimeError(
                        f'Unacceptable value for option active: {kwargs["active"]}'
                    )
            # verify input
            if "input" in kwargs and kwargs["input"] is not None:
                try:
                    ifiles = sos_targets(kwargs["input"])
                    for ifile in ifiles:
                        if not ifile.target_exists("target"):
                            raise RuntimeError(f"Input file {ifile} does not exist.")
                except Exception as e:
                    raise ValueError(
                        f'Unacceptable value ({kwargs["input"]}) for parameter input of actions: {e}'
                    )

            # if there are parameters input and output, the action is subject to signature verification
            sig = None
            # tracked can be True, filename or list of filename
            if (
                "tracked" in kwargs
                and kwargs["tracked"] is not None
                and kwargs["tracked"] is not False
            ):
                if args and isinstance(args[0], str):
                    script = args[0]
                elif "script" in kwargs:
                    script = kwargs["script"]
                else:
                    script = ""

                try:
                    tfiles = sos_targets(
                        [] if kwargs["tracked"] is True else kwargs["tracked"]
                    )
                except Exception as e:
                    raise ValueError(
                        f'Parameter tracked of actions can be None, True/False, or one or more filenames: {kwargs["tracked"]} provided: {e}'
                    )

                # append input and output
                for t in ("input", "output"):
                    if t in kwargs and kwargs[t] is not None:
                        tfiles.extend(sos_targets(kwargs[t]))

                from .targets import RuntimeInfo

                sig = RuntimeInfo(
                    textMD5(script),
                    sos_targets(kwargs["input"] if "input" in kwargs else []),
                    sos_targets(kwargs["output"] if "output" in kwargs else []),
                    sos_targets(
                        kwargs["tracked"]
                        if "tracked" in kwargs and kwargs["tracked"] is not True
                        else []
                    ),
                    kwargs,
                )
                sig.lock()
                if env.config["sig_mode"] in ("default", "skip", "distributed"):
                    matched = sig.validate()
                    if isinstance(matched, dict):
                        env.logger.info(
                            f"Action ``{func.__name__}`` is ``ignored`` due to saved signature"
                        )
                        return None
                    else:
                        env.logger.debug(f"Signature mismatch: {matched}")
                elif env.config["sig_mode"] == "assert":
                    matched = sig.validate()
                    if isinstance(matched, str):
                        raise RuntimeError(f"Signature mismatch: {matched}")
                    else:
                        env.logger.info(
                            f"Action ``{func.__name__}`` is ``ignored`` with matching signature"
                        )
                        return None
                elif env.config["sig_mode"] == "build":
                    # build signature require existence of files
                    if sig.write():
                        env.logger.info(
                            f"Action ``{func.__name__}`` is ``ignored`` with signature constructed"
                        )
                        return None
            original_env = {}
            if "default_env" in kwargs:
                original_env = copy.deepcopy(os.environ)
                if not isinstance(kwargs["default_env"], dict):
                    raise ValueError(
                        f'Option default_env must be a dictionary, {kwargs["default_env"]} provided'
                    )
                for k in kwargs["default_env"]:
                    if k not in os.environ:
                        os.environ[k] = kwargs["default_env"][k]
            if "env" in kwargs:
                original_env = copy.deepcopy(os.environ)
                if not isinstance(kwargs["env"], dict):
                    raise ValueError(
                        f'Option env must be a dictionary, {kwargs["env"]} provided'
                    )
                os.environ.update(kwargs["env"])
            # workdir refers to directory inside of docker image
            if "workdir" in kwargs:
                if not kwargs["workdir"] or not isinstance(
                    kwargs["workdir"], (str, os.PathLike)
                ):
                    raise RuntimeError(
                        f'workdir option should be a path of type str or path, {kwargs["workdir"]} provided'
                    )
                workdir = path(kwargs["workdir"])
                if not os.path.isdir(workdir):
                    os.makedirs(workdir, exist_ok=True)
                try:
                    olddir = os.getcwd()
                    os.chdir(workdir)
                    try:
                        res = func(*args, **kwargs)
                    except Exception as e:
                        if "allow_error" in kwargs and kwargs["allow_error"]:
                            env.logger.warning(str(e))
                            res = None
                        else:
                            raise
                finally:
                    os.chdir(olddir)
                    if original_env:
                        os.environ.clear()
                        os.environ.update(original_env)
            else:
                try:
                    res = func(*args, **kwargs)
                except Exception as e:
                    if "allow_error" in kwargs and kwargs["allow_error"]:
                        env.logger.warning(str(e))
                        res = None
                    else:
                        raise
                finally:
                    if original_env:
                        os.environ.clear()
                        os.environ.update(original_env)
            if "output" in kwargs and kwargs["output"] is not None:
                ofiles = sos_targets(kwargs["output"])
                for ofile in ofiles:
                    if not ofile.target_exists("any"):
                        raise RuntimeError(
                            f"Output target {ofile} does not exist after completion of action {func.__name__}"
                        )
            if sig:
                sig.write()
                sig.release()
            return res

        return action_wrapper

    return runtime_decorator


class SoS_ExecuteScript:
    def __init__(self, script, interpreter, suffix, args=""):
        self.script = script
        self.interpreter = interpreter
        self.args = args
        if suffix:
            self.suffix = suffix
        elif sys.platform == "win32":
            self.suffix = ".bat"
        else:
            self.suffix = ".sh"

    def process_template(self, cmd, filename, script, **kwargs):
        if "template" in kwargs:
            template = kwargs["template"]
        else:
            template_name = kwargs["template_name"]
            if "CONFIG" not in env.sos_dict:
                load_config_files()
            if (
                "action_templates" in env.sos_dict["CONFIG"]
                and template_name in env.sos_dict["CONFIG"]["action_templates"]
            ):
                template = env.sos_dict["CONFIG"]["action_templates"][template_name]
            elif template_name == "conda":
                template = textwrap.dedent(
                    """\
                    conda run -n {env_name} {cmd}
                    """
                )
            else:
                raise ValueError(
                    f'No template named {template_name} is built-in or provided in "action_templates" of config files.'
                )

        try:
            context = copy.deepcopy(kwargs)
            context["cmd"] = cmd
            context["filename"] = filename
            context["script"] = script
            return interpolate(template, context)
        except Exception as e:
            raise ValueError(f"Failed to expand template {template}: {e}")

    def run(self, **kwargs):
        #
        if "input" in kwargs:
            try:
                ifiles = sos_targets(kwargs["input"])
            except Exception as e:
                raise ValueError(
                    f'Unacceptable value ({kwargs["input"]}) for paremter input: {e}'
                )

            content = ""
            for ifile in ifiles:
                try:
                    with open(ifile) as iscript:
                        content += iscript.read()
                except Exception as e:
                    raise RuntimeError(f"Failed to read from {ifile}: {e}")
            self.script = content + self.script

        if "engine" in kwargs and kwargs["engine"] == "docker":
            from .docker.client import SoS_DockerClient

            docker = SoS_DockerClient()
            docker.run(
                kwargs["container"],
                self.script,
                self.interpreter,
                self.args,
                self.suffix,
                **kwargs,
            )
        elif "engine" in kwargs and kwargs["engine"] == "singularity":
            from .singularity.client import SoS_SingularityClient

            singularity = SoS_SingularityClient()
            singularity.run(
                kwargs["container"],
                self.script,
                self.interpreter,
                self.args,
                self.suffix,
                **kwargs,
            )
        else:
            if isinstance(self.interpreter, str):
                if self.interpreter and not shutil.which(
                    shlex.split(self.interpreter)[0]
                ):
                    raise RuntimeError(
                        f"Failed to locate interpreter {self.interpreter}"
                    )
            elif isinstance(self.interpreter, Sequence):
                found = False
                for ip in self.interpreter:
                    if shutil.which(shlex.split(ip)[0]):
                        self.interpreter = ip
                        found = True
                        break
                if not found:
                    raise RuntimeError(
                        f'Failed to locate any of the interpreters {", ".join(self.interpreter)}'
                    )
            else:
                raise RuntimeError(f"Unacceptable interpreter {self.interpreter}")

            debug_script_file = os.path.join(
                env.exec_dir,
                ".sos",
                f'{env.sos_dict["step_name"]}_{env.sos_dict["_index"]}_{str(uuid.uuid4())[:8]}{self.suffix}',
            )
            # with open(debug_script_file, 'w') as sfile:
            #    sfile.write(self.script)
            # env.log_to_file('ACTION', self.script)

            try:
                p = None
                script_file = tempfile.NamedTemporaryFile(
                    mode="w+t", suffix=self.suffix, delete=False
                ).name
                # potentially used for template
                cmd_file = None
                with open(script_file, "w") as sfile:
                    sfile.write(self.script)
                if not self.args:
                    self.args = "{filename:q}"
                # if no intepreter, let us prepare for the case when the script will be executed directly
                if not self.interpreter:
                    # make the script executable
                    os.chmod(script_file, 0o775)
                #
                if env.config["run_mode"] == "dryrun":
                    cmd = interpolate(
                        f"{self.interpreter} {self.args}",
                        {"filename": path("SCRIPT"), "script": self.script},
                    )
                    if "__std_out__" in env.sos_dict:
                        with open(env.sos_dict["__std_out__"], "a") as so:
                            so.write(f"HINT: {cmd}\n{self.script}\n")
                    else:
                        print(f"HINT: {cmd}\n{self.script}\n")
                    return None
                cmd = interpolate(
                    f"{self.interpreter} {self.args}",
                    {"filename": sos_targets(script_file), "script": self.script},
                )
                transcript_cmd = interpolate(
                    f"{self.interpreter} {self.args}",
                    {"filename": sos_targets("SCRIPT"), "script": self.script},
                )
                if "template_name" in kwargs or "template" in kwargs:
                    templated_script = self.process_template(
                        cmd, sos_targets(script_file), self.script, **kwargs
                    )
                    cmd_file = tempfile.NamedTemporaryFile(
                        mode="w+t",
                        suffix=".bat" if sys.platform == "win32" else ".sh",
                        delete=False,
                    ).name
                    with open(cmd_file, "w") as cfile:
                        cfile.write(templated_script)
                    # if it has an shebang line
                    if templated_script.startswith("#!") or sys.platform == "win32":
                        os.chmod(cmd_file, 0o775)
                        cmd = cmd_file
                    else:
                        cmd = f"sh {shlex.quote(cmd_file)}"
                    env.logger.debug(
                        f"Running templated script \n{templated_script}\ncommand {cmd}"
                    )
                transcribe(self.script, cmd=transcript_cmd)
                # if not notebook, not task, signature database is avaialble.
                if (
                    env.sos_dict["_index"] == 0
                    and env.config["run_mode"] != "interactive"
                    and "__std_out__" not in env.sos_dict
                    and hasattr(env, "master_push_socket")
                    and env.master_push_socket is not None
                ):
                    send_message_to_controller(
                        [
                            "workflow_sig",
                            "transcript",
                            env.sos_dict["step_name"],
                            repr(
                                {
                                    "start_time": time.time(),
                                    "command": transcript_cmd,
                                    "script": self.script,
                                }
                            ),
                        ]
                    )

                if env.config["run_mode"] == "interactive":
                    if "stdout" in kwargs or "stderr" in kwargs:
                        child = subprocess.Popen(
                            cmd,
                            shell=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            bufsize=0,
                        )
                        out, err = child.communicate()
                        if "stdout" in kwargs:
                            if kwargs["stdout"] is not False and len(out):
                                with open(kwargs["stdout"], "ab") as so:
                                    so.write(out)
                        else:
                            sys.stdout.write(out.decode())

                        if "stderr" in kwargs:
                            if kwargs["stderr"] is not False and len(err):
                                with open(kwargs["stderr"], "ab") as se:
                                    se.write(err)
                        else:
                            sys.stderr.write(err.decode())
                        ret = child.returncode
                    else:
                        # need to catch output and send to python output, which will in trun be hijacked by SoS notebook
                        from .utils import pexpect_run

                        ret = pexpect_run(cmd.strip())
                elif "__std_out__" in env.sos_dict and "__std_err__" in env.sos_dict:
                    if "stdout" in kwargs or "stderr" in kwargs:
                        if "stdout" in kwargs:
                            if kwargs["stdout"] is False:
                                so = subprocess.DEVNULL
                            else:
                                so = open(kwargs["stdout"], "ab")
                        elif env.verbosity > 0:
                            so = open(env.sos_dict["__std_out__"], "ab")
                        else:
                            so = subprocess.DEVNULL

                        if "stderr" in kwargs:
                            if kwargs["stderr"] is False:
                                se = subprocess.DEVNULL
                            else:
                                se = open(kwargs["stderr"], "ab")
                        elif env.verbosity > 1:
                            se = open(env.sos_dict["__std_err__"], "ab")
                        else:
                            se = subprocess.DEVNULL

                        p = subprocess.Popen(cmd, shell=True, stderr=se, stdout=so)
                        ret = p.wait()

                        if so != subprocess.DEVNULL:
                            so.close()
                        if se != subprocess.DEVNULL:
                            se.close()

                    elif env.verbosity >= 1:
                        with open(env.sos_dict["__std_out__"], "ab") as so, open(
                            env.sos_dict["__std_err__"], "ab"
                        ) as se:
                            p = subprocess.Popen(cmd, shell=True, stderr=se, stdout=so)
                            ret = p.wait()
                    else:
                        p = subprocess.Popen(
                            cmd,
                            shell=True,
                            stderr=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                        )
                        ret = p.wait()
                else:
                    if "stdout" in kwargs:
                        if kwargs["stdout"] is False:
                            so = subprocess.DEVNULL
                        else:
                            so = open(kwargs["stdout"], "ab")
                    elif env.verbosity > 0:
                        so = None
                    else:
                        so = subprocess.DEVNULL

                    if "stderr" in kwargs:
                        if kwargs["stderr"] is False:
                            se = subprocess.DEVNULL
                        else:
                            se = open(kwargs["stderr"], "ab")
                    elif env.verbosity > 1:
                        se = None
                    else:
                        se = subprocess.DEVNULL

                    p = subprocess.Popen(cmd, shell=True, stderr=se, stdout=so)

                    ret = p.wait()
                    if so is not None and so != subprocess.DEVNULL:
                        so.close()
                    if se is not None and se != subprocess.DEVNULL:
                        se.close()
                # clean up empty stdstream files
                for item in ["stdout", "stderr"]:
                    if (
                        item in kwargs
                        and os.path.isfile(kwargs[item])
                        and os.path.getsize(kwargs[item]) == 0
                    ):
                        try:
                            os.remove(kwargs[item])
                        except Exception:
                            pass
                if ret != 0:
                    with open(debug_script_file, "w") as sfile:
                        sfile.write(self.script)
                    cmd = cmd.replace(script_file, f".sos/{path(debug_script_file):b}")
                    out = (
                        f", stdout={kwargs['stdout']}"
                        if "stdout" in kwargs
                        and os.path.isfile(kwargs["stdout"])
                        and os.path.getsize(kwargs["stdout"]) > 0
                        else ""
                    )
                    err = (
                        f", stderr={kwargs['stderr']}"
                        if "stderr" in kwargs
                        and os.path.isfile(kwargs["stderr"])
                        and os.path.getsize(kwargs["stderr"]) > 0
                        else ""
                    )
                    raise subprocess.CalledProcessError(
                        returncode=ret,
                        cmd=cmd,
                        stderr="\nFailed to execute ``{}``\nexitcode={}, workdir=``{}``{}{}{}\n{}".format(
                            cmd,
                            ret,
                            os.getcwd(),
                            f', task={os.path.basename(env.sos_dict["__std_err__"]).split(".")[0]}'
                            if "__std_err__" in env.sos_dict
                            else "",
                            out,
                            err,
                            "-" * 75,
                        ),
                    )
            finally:
                try:
                    os.remove(script_file)
                except Exception:
                    # 1315: ignore in case the temp script file no longer exists
                    pass
                if cmd_file is not None:
                    try:
                        os.remove(cmd_file)
                    except Exception:
                        # 1315: ignore in case the temp script file no longer exists
                        pass


@SoS_Action()
def sos_run(workflow=None, targets=None, shared=None, args=None, source=None, **kwargs):
    """Execute a workflow from the current SoS script or a specified source
    (in .sos or .ipynb format), with _input as the initial input of workflow."""
    if "__std_out__" in env.sos_dict and "__std_err__" in env.sos_dict:
        raise RuntimeError(
            "Executing nested workflow (action sos_run) in tasks is not supported."
        )

    if isinstance(workflow, str):
        workflows = [workflow]
    elif isinstance(workflow, Sequence):
        workflows = list(workflow)
    elif workflow is None:
        workflows = []
    else:
        raise ValueError(
            "workflow has to be None, a workflow name, or a list of workflow names"
        )

    if source is None:
        script = SoS_Script(
            env.sos_dict["__step_context__"].content,
            env.sos_dict["__step_context__"].filename,
        )
        if workflows:
            wfs = [script.workflow(wf, use_default=True) for wf in workflows]
        else:
            wfs = [script.workflow(use_default=False)]
    else:
        # reading workflow from another file
        script = SoS_Script(filename=source)

        if workflows:
            wfs = [script.workflow(wf, use_default=True) for wf in workflows]
        else:
            wfs = [script.workflow(use_default=False)]

    # if wf contains the current step or one of the previous one, this constitute
    # recusive nested workflow and should not be allowed
    all_parameters = set()
    for wf in wfs:
        all_parameters |= set(wf.parameters())
        if env.sos_dict["step_name"] in [f"{x.name}_{x.index}" for x in wf.sections]:
            raise RuntimeError(
                f'Nested workflow {workflow} contains the current step {env.sos_dict["step_name"]}'
            )

    # args can be specified both as a dictionary or keyword arguments
    if args is None:
        args = kwargs
    else:
        args.update(kwargs)

    for key in args.keys():
        if key not in all_parameters and key not in SOS_ACTION_OPTIONS:
            raise ValueError(f"No parameter {key} is defined for workflow {workflow}")

    if shared is None:
        shared = []
    elif isinstance(shared, str):
        shared = [shared]

    # for nested workflow, _input would becomes the input of workflow.
    env.sos_dict.set("__step_output__", copy.deepcopy(env.sos_dict.get("_input", None)))
    shared.append("__step_output__")
    try:
        my_name = env.sos_dict["step_name"]
        args_output = ", ".join(
            f"{x}={short_repr(y)}" for x, y in args.items() if not x.startswith("__")
        )
        if "ACTION" in env.config["SOS_DEBUG"] or "ALL" in env.config["SOS_DEBUG"]:
            env.log_to_file(
                "ACTION",
                "Executing workflow ``{}`` with input ``{}`` and {}".format(
                    workflow,
                    short_repr(env.sos_dict.get("_input", None), True),
                    "no args" if not args_output else args_output,
                ),
            )

        if not hasattr(env, "__socket__") or env.__socket__ is None:
            raise RuntimeError("sos_run function cannot be executed in scratch cell.")
        # tell the master process to receive a workflow
        # really send the workflow
        shared = {x: (env.sos_dict[x] if x in env.sos_dict else None) for x in shared}

        wf_ids = [str(uuid.uuid4()) for wf in wfs]

        blocking = not env.sos_dict.get("__concurrent_subworkflow__", False)
        env.__socket__.send(
            encode_msg(
                ["workflow", wf_ids, wfs, targets, args, shared, env.config, blocking]
            )
        )

        if not blocking:
            return {"pending_workflows": wf_ids}
        res = {}
        for wf in wfs:
            wf_res = decode_msg(env.__socket__.recv())
            res.update(wf_res)
            if wf_res is None:
                sys.exit(0)
            elif isinstance(wf_res, Exception):
                raise wf_res
            else:
                env.sos_dict.quick_update(wf_res["shared"])
        return res
    finally:
        # restore step_name in case the subworkflow re-defines it
        env.sos_dict.set("step_name", my_name)


@SoS_Action(acceptable_args=["script", "interpreter", "suffix", "args"])
def script(script, interpreter="", suffix="", args="", **kwargs):
    """Execute specified script using specified interpreter. This action accepts common
    action arguments such as input, active, workdir, docker_image and args. In particular,
    content of one or more files specified by option input would be prepended before
    the specified script."""
    return SoS_ExecuteScript(script, interpreter, suffix, args).run(**kwargs)


@SoS_Action(acceptable_args=["expr", "msg"])
def fail_if(expr, msg=""):
    """Raise an exception with `msg` if condition `expr` is False"""
    if expr:
        raise TerminateExecution(msg if msg else "error triggered by action fail_if")
    return 0


@SoS_Action(acceptable_args=["expr", "msg"])
def warn_if(expr, msg=""):
    """Yield an warning message `msg` if `expr` is False """
    if expr:
        env.logger.warning(msg)
    return 0


@SoS_Action(acceptable_args=["expr", "msg", "no_output"])
def stop_if(expr, msg="", no_output=False):
    """Abort the execution of the current step or loop and yield
    an warning message `msg` if `expr` is False"""
    if expr:
        raise StopInputGroup(msg=msg, keep_output=not no_output)
    return 0


@SoS_Action(acceptable_args=["expr", "msg"])
def done_if(expr, msg=""):
    """Assuming that output has already been generated and stop
    executing the rest of the substep"""
    if expr:
        raise StopInputGroup(msg=msg, keep_output=True)
    return 0


@SoS_Action(acceptable_args=["expr", "msg", "no_output"])
def skip_if(expr, msg=""):
    """Skip the current substep and set _output to empty. Output
    will be removed if already generated."""
    if expr:
        raise StopInputGroup(msg=msg, keep_output=False)
    return 0


#
# download file with progress bar
#


def downloadURL(URL, dest, decompress=False, index=None):
    dest = os.path.abspath(os.path.expanduser(dest))
    dest_dir, filename = os.path.split(dest)
    #
    if not os.path.isdir(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
    if not os.path.isdir(dest_dir):
        raise RuntimeError(f"Failed to create destination directory to download {URL}")
    #
    message = filename
    if len(message) > 30:
        message = message[:10] + "..." + message[-16:]
    #
    dest_tmp = dest + f".tmp_{os.getpid()}"
    term_width = shutil.get_terminal_size((80, 20)).columns
    try:
        env.logger.debug(f"Download {URL} to {dest}")
        sig = file_target(dest)
        if os.path.isfile(dest):
            prog = ProgressBar(
                desc=message,
                disable=env.verbosity <= 1,
                position=index,
                leave=True,
                bar_format="{desc}",
                total=10000000,
            )
            target = file_target(dest)
            if env.config["sig_mode"] == "build":
                prog.set_description(message + ": \033[32m writing signature\033[0m")
                prog.update()
                target.write_sig()
                prog.close()
                return True
            elif env.config["sig_mode"] == "ignore":
                prog.set_description(message + ": \033[32m use existing\033[0m")
                prog.update()
                prog.close()
                return True
            elif env.config["sig_mode"] in ("default", "skip", "distributed"):
                prog.update()
                if sig.validate():
                    prog.set_description(message + ": \033[32m Validated\033[0m")
                    prog.update()
                    prog.close()
                    return True
                else:
                    prog.set_description(
                        message + ":\033[91m Signature mismatch\033[0m"
                    )
                    target.write_sig()
                    prog.update()
        #
        prog = ProgressBar(
            desc=message,
            disable=env.verbosity <= 1,
            position=index,
            leave=True,
            bar_format="{desc}",
            total=10000000,
        )
        #
        # Stop using pycurl because of libcurl version compatibility problems
        # that happen so often and difficult to fix. Error message looks like
        #
        # Reason: Incompatible library version: pycurl.cpython-35m-darwin.so
        # requires version 9.0.0 or later, but libcurl.4.dylib provides version 7.0.0
        #
        # with open(dest_tmp, 'wb') as f:
        #    c = pycurl.Curl()
        #    c.setopt(pycurl.URL, str(URL))
        #    c.setopt(pycurl.WRITEFUNCTION, f.write)
        #    c.setopt(pycurl.SSL_VERIFYPEER, False)
        #    c.setopt(pycurl.NOPROGRESS, False)
        #    c.setopt(pycurl.PROGRESSFUNCTION, prog.curlUpdate)
        #    c.perform()
        # if c.getinfo(pycurl.HTTP_CODE) == 404:
        #    prog.set_description(message + ':\033[91m 404 Error {}\033[0m'.format(' '*(term_width - len(message) - 12)))
        #    try:
        #        os.remove(dest_tmp)
        #    except OSError:
        #        pass
        #    return False
        with open(dest_tmp, "wb") as f:
            try:
                u = urllib.request.urlopen(str(URL))
                try:
                    file_size = int(u.getheader("Content-Length"))
                    prog = ProgressBar(
                        total=file_size, desc=message, position=index, leave=False
                    )
                except Exception:
                    file_size = None
                file_size_dl = 0
                block_sz = 8192
                while True:
                    buffer = u.read(block_sz)
                    if not buffer:
                        break
                    file_size_dl += len(buffer)
                    f.write(buffer)
                    prog.update(len(buffer))
            except urllib.error.HTTPError as e:
                prog.set_description(message + f":\033[91m {e.code} Error\033[0m")
                prog.update()
                prog.close()
                try:
                    os.remove(dest_tmp)
                except OSError:
                    pass
                return False
            except Exception as e:
                prog.set_description(message + f":\033[91m {e}\033[0m")
                prog.update()
                prog.close()
                try:
                    os.remove(dest_tmp)
                except OSError:
                    pass
                return False
        #
        if os.path.isfile(dest):
            os.remove(dest)
        os.rename(dest_tmp, dest)
        decompressed = 0
        if decompress:
            if zipfile.is_zipfile(dest):
                prog.set_description(message + ":\033[91m Decompressing\033[0m")
                prog.update()
                prog.close()
                zfile = zipfile.ZipFile(dest)
                zfile.extractall(dest_dir)
                names = zfile.namelist()
                for name in names:
                    if os.path.isdir(os.path.join(dest_dir, name)):
                        continue
                    elif not os.path.isfile(os.path.join(dest_dir, name)):
                        return False
                    else:
                        decompressed += 1
            elif tarfile.is_tarfile(dest):
                prog.set_description(message + ":\033[91m Decompressing\033[0m")
                prog.update()
                prog.close()
                with tarfile.open(dest, "r:*") as tar:
                    tar.extractall(dest_dir)
                    # only extract files
                    files = [x.name for x in tar.getmembers() if x.isfile()]
                    for name in files:
                        if not os.path.isfile(os.path.join(dest_dir, name)):
                            return False
                        else:
                            decompressed += 1
            elif dest.endswith(".gz"):
                prog.set_description(message + ":\033[91m Decompressing\033[0m")
                prog.update()
                prog.close()
                decomp = dest[:-3]
                with gzip.open(dest, "rb") as fin, open(decomp, "wb") as fout:
                    buffer = fin.read(100000)
                    while buffer:
                        fout.write(buffer)
                        buffer = fin.read(100000)
                decompressed += 1
        decompress_msg = (
            ""
            if not decompressed
            else f' ({decompressed} file{"" if decompressed <= 1 else "s"} decompressed)'
        )
        prog.set_description(
            message
            + f':\033[32m downloaded{decompress_msg} {" "*(term_width - len(message) - 13 - len(decompress_msg))}\033[0m'
        )
        prog.update()
        prog.close()
        # if a md5 file exists
        # if downloaded files contains .md5 signature, use them to validate
        # downloaded files.
        if os.path.isfile(dest + ".md5"):
            prog.set_description(message + ":\033[91m Verifying md5 signature\033[0m")
            prog.update()
            prog.close()
            with open(dest + ".md5") as md5:
                rec_md5 = md5.readline().split()[0].strip()
                obs_md5 = fileMD5(dest, partial=False)
                if rec_md5 != obs_md5:
                    prog.set_description(
                        message + ":\033[91m MD5 signature mismatch\033[0m"
                    )
                    prog.update()
                    prog.close()
                    env.logger.warning(
                        f"md5 signature mismatch for downloaded file {filename[:-4]} (recorded {rec_md5}, observed {obs_md5})"
                    )
            prog.set_description(message + ":\033[91m MD5 signature verified\033[0m")
            prog.update()
            prog.close()
    except Exception as e:
        if env.verbosity > 2:
            sys.stderr.write(get_traceback())
        env.logger.error(f"Failed to download: {e}")
        return False
    finally:
        # if there is something wrong still remove temporary file
        if os.path.isfile(dest_tmp):
            os.remove(dest_tmp)
    return os.path.isfile(dest)


@SoS_Action(
    acceptable_args=[
        "URLs",
        "workdir",
        "dest_dir",
        "dest_file",
        "decompress",
        "max_jobs",
    ]
)
def download(URLs, dest_dir=".", dest_file=None, decompress=False, max_jobs=5):
    """Download files from specified URL, which should be space, tab or
    newline separated URLs. The files will be downloaded to specified destination.
    Option "dest_dir" specify the destination directory,
    and "dest_file" specify the output filename, which will otherwise be the same
    specified in the URL. If `filename.md5` files are downloaded, they are used to
    validate downloaded `filename`. If "decompress=True", compressed
    files are decompressed. If `max_jobs` is given, a maximum of `max_jobs`
    concurrent download jobs will be used for each domain. This restriction
    applies to domain names and will be applied to multiple download
    instances.
    """
    if env.config["run_mode"] == "dryrun":
        print(f"HINT: download\n{URLs}\n")
        return None
    if isinstance(URLs, str):
        urls = [x.strip() for x in URLs.split() if x.strip()]
    else:
        urls = list(URLs)

    if not urls:
        env.logger.debug(f"No download URL specified: {URLs}")
        return
    #
    if dest_file is not None and len(urls) != 1:
        raise RuntimeError(
            "Only one URL is allowed if a destination file is specified."
        )
    #
    if dest_file is None:
        filenames = []
        for idx, url in enumerate(urls):
            token = urllib.parse.urlparse(url)
            # if no scheme or netloc, the URL is not acceptable
            if not all(
                [
                    getattr(token, qualifying_attr)
                    for qualifying_attr in ("scheme", "netloc")
                ]
            ):
                raise ValueError(f"Invalid URL {url}")
            filename = os.path.split(token.path)[-1]
            if not filename:
                raise ValueError(f"Cannot determine destination file for {url}")
            filenames.append(os.path.join(dest_dir, filename))
    else:
        token = urllib.parse.urlparse(urls[0])
        if not all(
            [
                getattr(token, qualifying_attr)
                for qualifying_attr in ("scheme", "netloc")
            ]
        ):
            raise ValueError(f"Invalid URL {url}")
        filenames = [dest_file]
    #
    succ = [(False, None) for x in urls]
    with ProcessPoolExecutor(max_workers=max_jobs) as executor:
        for idx, (url, filename) in enumerate(zip(urls, filenames)):
            # if there is alot, start download
            succ[idx] = executor.submit(downloadURL, url, filename, decompress, idx)
    succ = [x.result() for x in succ]

    # for su, url in zip(succ, urls):
    #    if not su:
    #        env.logger.warning('Failed to download {}'.format(url))
    failed = [y for x, y in zip(succ, urls) if not x]
    if failed:
        if len(urls) == 1:
            raise RuntimeError("Failed to download {urls[0]}")
        else:
            raise RuntimeError(
                f"Failed to download {failed[0]} ({len(failed)} out of {len(urls)})"
            )
    return 0


@SoS_Action(acceptable_args=["script", "args"])
def run(script, args="", **kwargs):
    """Execute specified script using bash. This action accepts common action arguments such as
    input, active, workdir, docker_image and args. In particular, content of one or more files
    specified by option input would be prepended before the specified script."""
    if sys.platform == "win32":
        # in the case there is no interpreter, we put the script
        # at first (this is the case for windows)
        # and we donot add default args.
        interpreter = ""
    else:
        # if there is a shebang line, we ...
        if not script.startswith("#!"):
            interpreter = "/bin/bash"
            if not args:
                args = "-ev {filename:q}"
        else:
            # execute script directly
            interpreter = ""
    return SoS_ExecuteScript(script, interpreter, "", args).run(**kwargs)


@SoS_Action(acceptable_args=["script", "args"])
def perl(script, args="", **kwargs):
    """Execute specified script using perl. This action accepts common action arguments such as
    input, active, workdir, docker_image and args. In particular, content of one or more files
    specified by option input would be prepended before the specified script."""
    return SoS_ExecuteScript(script, "perl", ".pl", args).run(**kwargs)


def collect_input(script, input):
    # determine file extension
    if input is not None:
        if isinstance(input, (str, file_target)):
            ext = os.path.splitext(input)[-1]
        elif isinstance(input, Sequence) and len(input) > 0:
            ext = os.path.splitext(input[0])[-1]
        else:
            raise ValueError("Unknown input file for action pandoc")
    else:
        ext = ".md"

    input_file = tempfile.NamedTemporaryFile(mode="w+t", suffix=ext, delete=False).name
    with open(input_file, "w") as tmp:
        if script is not None and script.strip():
            tmp.write(script.rstrip() + "\n\n")
        if isinstance(input, str):
            try:
                with open(input) as ifile:
                    tmp.write(ifile.read() + "\n\n")
            except Exception as e:
                raise ValueError(f"Failed to read input file {input}: {e}")
        elif isinstance(input, Sequence):
            for ifile in input:
                try:
                    with open(ifile) as itmp:
                        tmp.write(itmp.read().rstrip() + "\n\n")
                except Exception as e:
                    raise ValueError(f"Failed to read input file {ifile}: {e}")
    return input_file


@SoS_Action(acceptable_args=["script"])
def report(script=None, input=None, output=None, **kwargs):
    """Write script to an output file specified by `output`, which can be
    a filename to which the content of the script will be written,
    any object with a "write" attribute (e.g. a file handle) for which the "write"
    function will be called with the content. If output is unspecified, the content
    will be written to standard output or appended to a file specified with command
    line option `-r`."""
    if env.config["run_mode"] == "dryrun":
        if "__std_out__" in env.sos_dict:
            with open(env.sos_dict["__std_out__"], "a") as so:
                so.write(f'HINT: report:\n{"" if script is None else script}\n')
                if input is not None:
                    for ifile in input:
                        so.write(f"  from file: {ifile}\n")
        else:
            print(f'HINT: report:\n{"" if script is None else script}')
            if input is not None:
                for ifile in input:
                    print(f"  from file: {ifile}")
        return
    file_handle = None
    if isinstance(output, str):
        if not output or output == "-":
            writer = sys.stdout.write
        elif output.startswith(">>"):
            file_handle = open(os.path.expanduser(output[2:]), "a")
            writer = file_handle.write
        else:
            file_handle = open(os.path.expanduser(output), "w")
            writer = file_handle.write
    elif isinstance(output, (path, file_target)):
        file_handle = open(os.path.expanduser(str(output)), "w")
        writer = file_handle.write
    elif isinstance(output, (paths, sos_targets)):
        if len(output) != 1:
            raise ValueError(f"More than one output is specified {output}")
        if not isinstance(output[0], (file_target, path)):
            raise ValueError(
                f"Action report can only output to file target or standard output"
            )
        file_handle = open(os.path.expanduser(str(output[0])), "w")
        writer = file_handle.write
    elif hasattr(output, "write"):
        writer = output.write
    elif output is None or output == "":
        writer = sys.stdout.write
    else:
        raise ValueError(f"Invalid output {output}.")

    # file lock to prevent race condition
    with TimeoutInterProcessLock(os.path.join(env.temp_dir, "report_lock")):
        if isinstance(script, str) and script.strip():
            writer(script.rstrip() + "\n\n")
        if input is not None:
            if isinstance(input, (str, file_target)):
                if (
                    "ACTION" in env.config["SOS_DEBUG"]
                    or "ALL" in env.config["SOS_DEBUG"]
                ):
                    env.log_to_file("ACTION", f"Loading report from {input}")
                with open(input) as ifile:
                    writer(ifile.read().rstrip() + "\n\n")
            elif isinstance(input, Sequence):
                for ifile in input:
                    try:
                        env.logger.debug(f"Loading report from {ifile}")
                        with open(ifile) as itmp:
                            writer(itmp.read().rstrip() + "\n\n")
                    except Exception as e:
                        raise ValueError(f"Failed to read input file {ifile}: {e}")
            else:
                raise ValueError("Unknown input file for action report")
    #
    if file_handle:
        file_handle.close()


@SoS_Action(acceptable_args=["script", "args"])
def pandoc(
    script=None, input=None, output=None, args="{input:q} --output {output:q}", **kwargs
):
    """Convert input file to output using pandoc

    The input can be specified in three ways:

    1. instant script, which is assumed to be in md format

    pandoc:   output='report.html'
      script

    2. one or more input files. The format is determined by extension of input file

    pandoc(input, output='report.html')

    3. input file specified by command line option `-r` .
    pandoc(output='report.html')

    If no output is specified, it is assumed to be in html format
    and is written to standard output.

    You can specify more options such as "from" and "to" by customizing
    the args parameter of the action. The default value of args is
    `{input:q} --output {output:q}'
    """
    #
    #     # this is output format
    #     pandoc [OPTIONS] [FILES]
    # Input formats:  commonmark, docbook, docx, epub, haddock, html, json*, latex,
    #                 markdown, markdown_github, markdown_mmd, markdown_phpextra,
    #                 markdown_strict, mediawiki, native, odt, opml, org, rst, t2t,
    #                 textile, twiki
    #                 [ *only Pandoc's JSON version of native AST]
    # Output formats: asciidoc, beamer, commonmark, context, docbook, docx, dokuwiki,
    #                 dzslides, epub, epub3, fb2, haddock, html, html5, icml, json*,
    #                 latex, man, markdown, markdown_github, markdown_mmd,
    #                 markdown_phpextra, markdown_strict, mediawiki, native, odt,
    #                 opendocument, opml, org, pdf**, plain, revealjs, rst, rtf, s5,
    #                 slideous, slidy, tei, texinfo, textile
    #                 [**for pdf output, use latex or beamer and -o FILENAME.pdf]
    # Options:
    #   -f FORMAT, -r FORMAT  --from=FORMAT, --read=FORMAT
    #   -t FORMAT, -w FORMAT  --to=FORMAT, --write=FORMAT
    #   -o FILENAME           --output=FILENAME
    #                         --data-dir=DIRECTORY
    #   -R                    --parse-raw
    #   -S                    --smart
    #
    # IGNORED
    #
    if not executable("pandoc").target_exists():
        raise RuntimeError("pandoc not found")

    input = sos_targets(collect_input(script, input))

    output = sos_targets(output)
    if len(output) == 0:
        write_to_stdout = True
        output = sos_targets(
            tempfile.NamedTemporaryFile(mode="w+t", suffix=".html", delete=False).name
        )
    else:
        write_to_stdout = False
    #
    ret = 1
    try:
        p = None
        cmd = interpolate(f"pandoc {args}", {"input": input, "output": output})
        if "ACTION" in env.config["SOS_DEBUG"] or "ALL" in env.config["SOS_DEBUG"]:
            env.log_to_file("ACTION", f'Running command "{cmd}"')
        if env.config["run_mode"] == "interactive":
            # need to catch output and send to python output, which will in trun be hijacked by SoS notebook
            from .utils import pexpect_run

            ret = pexpect_run(cmd)
        else:
            p = subprocess.Popen(cmd, shell=True)
            ret = p.wait()
    except Exception as e:
        env.logger.error(str(e))
    if ret != 0:
        temp_file = os.path.join(".sos", f"pandoc_{os.getpid()}.md")
        shutil.copyfile(input, temp_file)
        cmd = interpolate(
            f"pandoc {args}",
            {"input": sos_targets(temp_file), "output": sos_targets(output)},
        )
        raise RuntimeError(
            f"Failed to execute script. Please use command \n{cmd}\nunder {os.getcwd()} to test it."
        )
    if write_to_stdout:
        with open(output[0].fullname()) as out:
            sys.stdout.write(out.read())
    else:
        env.logger.info(f"Report saved to {output}")
    try:
        os.remove(input)
    except Exception:
        pass
