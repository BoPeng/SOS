#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

#
# Utility functions used by various executors.
#
import os
import sys
import re
import psutil
import traceback

from typing import Any
from collections.abc import Sequence
from io import StringIO
from tokenize import generate_tokens

from .targets import (
    RemovedTarget,
    file_target,
    sos_targets,
    sos_step,
    path,
    dynamic,
    sos_variable,
    RuntimeInfo,
    textMD5,
)
from .utils import (
    env,
    Error,
    format_HHMMSS,
    expand_size,
    load_config_files,
    get_traceback,
)
from .eval import SoS_eval, stmtHash, analyze_global_statements
from .tasks import TaskParams
from .syntax import SOS_TAG
from .controller import request_answer_from_controller


class ExecuteError(Error):
    """An exception to collect exceptions raised during run time so that
    other branches of the DAG would continue if some nodes fail to execute."""

    def __init__(self, workflow: str) -> None:
        Error.__init__(self)
        self.workflow = workflow
        self.errors = []
        self.traces = []
        self.args = (workflow,)

    def append(self, line: str, error: Exception) -> None:
        lines = [x for x in line.split("\n") if x.strip()]
        if not lines:
            short_line = ""
        else:
            short_line = (
                "[" + (lines[0][:40] if len(lines[0]) > 40 else lines[0]) + "]:"
            )
        self.errors.append(short_line)
        self.traces.append(get_traceback())
        newline = "\n" if self.message else ""
        self.message += f"{newline}{short_line} {error}"


def __null_func__(*args, **kwargs) -> Any:
    """This function will be passed to SoS's namespace and be executed
    to evaluate functions of input, output, and depends directives."""

    def _flatten(x):
        if isinstance(x, str):
            return [x]
        elif isinstance(x, sos_targets):
            return [x]
        elif isinstance(x, Sequence):
            return sum((_flatten(k) for k in x), [])
        elif hasattr(x, "__flattenable__"):
            return _flatten(x.flatten())
        else:
            return [x]

    return _flatten(args), kwargs


def __output_from__(
    steps,
    group_by="auto",
    paired_with=None,
    pattern=None,
    group_with=None,
    for_each=None,
    remove_empty_groups=False,
):
    targets = sos_targets()
    if isinstance(steps, (int, str)):
        steps = [steps]
    elif isinstance(steps, Sequence):
        steps = list(steps)
    else:
        raise ValueError(f"Unacceptable value of input prameter from: {steps} provided")
    #
    for step in steps:
        if isinstance(step, int):
            if step == -1:
                # this refers to the last step of a forward style workflow
                if "__last_step__" in env.sos_dict and env.sos_dict["__last_step__"]:
                    step = env.sos_dict["__last_step__"]
                else:
                    raise ValueError(
                        f"output_from(-1) is called for a step without previous step"
                    )
            elif "_" in env.sos_dict["step_name"]:
                step = f"{env.sos_dict['step_name'].rsplit('_', 1)[0]}_{step}"
            else:
                step = str(step)
        res = request_answer_from_controller(["step_output", step])
        if res is None or not isinstance(res, sos_targets):
            raise RuntimeError(f"Failed to obtain output of step {step}")
        targets.extend(res)

    if group_by != "auto" or paired_with or pattern or group_with or for_each:
        targets = sos_targets(
            targets,
            group_by=group_by,
            paired_with=paired_with,
            pattern=pattern,
            group_with=group_with,
            for_each=for_each,
        )
    return targets._remove_empty_groups() if remove_empty_groups else targets


def __traced__(*args, **kwargs):
    return sos_targets(*args, **kwargs).set_traced()


def __named_output__(
    name,
    group_by="auto",
    paired_with=None,
    pattern=None,
    group_with=None,
    for_each=None,
    remove_empty_groups=False,
):
    targets = request_answer_from_controller(["named_output", name])
    if targets is None:
        env.logger.warning(f'named_output("{name}") is not found')
        return sos_targets([])

    if group_by != "auto" or paired_with or pattern or group_with or for_each:
        targets = sos_targets(
            targets,
            group_by=group_by,
            paired_with=paired_with,
            pattern=pattern,
            group_with=group_with,
            for_each=for_each,
        )
    return targets._remove_empty_groups() if remove_empty_groups else targets


def clear_output(output=None):
    """
    Remove file targets in `_output` when a step fails to complete
    """
    for target in env.sos_dict["_output"] if output is None else output:
        if isinstance(target, file_target) and target.exists():
            try:
                target.unlink()
            except Exception as e:
                env.logger.warning(f"Failed to remove {target}: {e}")


def get_traceback_msg(e):
    error_class = e.__class__.__name__
    tb = sys.exc_info()[-1]
    msg = ""
    for st in reversed(traceback.extract_tb(tb)):
        if st.filename.startswith("script_"):
            code = stmtHash.script(st.filename)
            line_number = st.lineno
            code = "\n".join(
                [
                    f'{"---->" if i+1 == line_number else "     "} {x.rstrip()}'
                    for i, x in enumerate(code.splitlines())
                ][max(line_number - 3, 0) : line_number + 3]
            )
            msg += f"""\
{st.filename} in {st.name}
{code}
"""
    detail = e.args[0] if e.args else ""
    if msg:
        return f"""
---------------------------------------------------------------------------
{error_class:42}Traceback (most recent call last)
{msg}
{error_class}: {detail}"""
    else:
        return f"{error_class}: {detail}"


def prepare_env(gdef="", gvars={}, extra_vars={}, host="localhost"):
    """clear current sos_dict, execute global_def (definitions and imports),
    and inject global variables"""
    env.sos_dict.clear()

    if not gdef and not gvars:
        # SoS Notebook calls prepare_env without global statement from a
        # particular
        gdef, gvars = analyze_global_statements("")

    if gdef:
        exec(compile(gdef, filename="<ast>", mode="exec"), env.sos_dict._dict)

    env.sos_dict.quick_update(gvars)
    env.sos_dict.quick_update(extra_vars)
    if "CONFIG" not in env.sos_dict:
        # if this is in sos notebook
        load_config_files()
    if (
        "hosts" not in env.sos_dict["CONFIG"]
        and "localhost" not in env.sos_dict["CONFIG"]
    ):
        env.sos_dict["CONFIG"]["localhost"] = "localhost"
        env.sos_dict["CONFIG"]["hosts"] = {
            "localhost": {"paths": {}, "address": "localhost"}
        }
    # expose `paths` of localhost
    if host == "localhost":
        if "localhost" in env.sos_dict["CONFIG"]:
            if (
                "hosts" not in env.sos_dict["CONFIG"]
                or env.sos_dict["CONFIG"]["localhost"]
                not in env.sos_dict["CONFIG"]["hosts"]
            ):
                env.logger.warning(
                    f"Localhost {env.sos_dict['CONFIG']['localhost']} is not defined in CONFIG['hosts']"
                )
                env.sos_dict["CONFIG"]["hosts"][env.sos_dict["CONFIG"]["localhost"]] = {
                    "paths": {},
                    "address": "localhost",
                }
            env.sos_dict.set("__host__", env.sos_dict["CONFIG"]["localhost"])
        else:
            if "hosts" in env.sos_dict["CONFIG"]:
                if "localhost" not in env.sos_dict["CONFIG"]["hosts"]:
                    env.logger.warning('locahost is not defined in "hosts".')
                    env.sos_dict["CONFIG"]["hosts"]["localhost"] = {
                        "paths": {},
                        "address": "localhost",
                    }
            elif "paths" not in env.sos_dict["CONFIG"]["hosts"]["localhost"]:
                env.sos_dict["CONFIG"]["hosts"]["localhost"]["paths"] = {}
            env.sos_dict.set("__host__", "localhost")
    else:
        if (
            "hosts" not in env.sos_dict["CONFIG"]
            or host not in env.sos_dict["CONFIG"]["hosts"]
        ):
            raise RuntimeError(
                f"Remote host {host} is not defined in CONFIG['hosts']. Available ones are {env.sos_dict['CONFIG']['hosts'].keys()}"
            )
        env.sos_dict.set("__host__", host)


def statementMD5(stmts):
    def _get_tokens(statement):
        return [
            x[1]
            for x in generate_tokens(StringIO(statement).readline)
            if x[1] not in ("", "\n")
        ]

    tokens = []
    for stmt in stmts:
        if stmt:
            tokens.extend(_get_tokens(stmt))
    return textMD5(" ".join(tokens))


def create_task(global_def, global_vars, task_stmt, task_params):
    # env.sos_dict.set('_runtime', {})
    if task_params:
        args, kwargs = SoS_eval(
            f"__null_func__({task_params})", extra_dict={"__null_func__": __null_func__}
        )
        if args:
            raise RuntimeError(
                f'Only keyword arguments are accepted for task statement: "{task_params}" provided'
            )
        for k, v in kwargs.items():
            # if k not in SOS_RUNTIME_OPTIONS:
            #     raise RuntimeError(f'Unrecognized runtime option {k}={v}')
            # standardize walltime to an integer
            if k == "walltime":
                v = format_HHMMSS(v)
            elif k == "mem":
                v = expand_size(v)
            env.sos_dict["_runtime"][k] = v
    #
    # we need to record the verbosity and sigmode of task during creation because
    # they might be changed while the task is in the queue waiting to be
    # submitted (this happens when tasks are submitted from Jupyter)
    env.sos_dict["_runtime"]["verbosity"] = env.verbosity
    env.sos_dict["_runtime"]["sig_mode"] = env.config.get("sig_mode", "default")
    env.sos_dict["_runtime"]["run_mode"] = env.config.get("run_mode", "run")
    #if "workdir" not in env.sos_dict["_runtime"]:
    #    env.sos_dict["_runtime"]["workdir"] = path.cwd().shrink()
    # elif "TASK" in env.config["SOS_DEBUG"] or "ALL" in env.config["SOS_DEBUG"]:
    #     env.log_to_file(
    #         "TASK", f'Using specified workdir {env.sos_dict["_runtime"]["workdir"]}'
    #     )

    # 1324
    env.sos_dict["_runtime"].update(env.config.get("queue_args", {}))

    # NOTE: we do not explicitly include 'step_input', 'step_output',
    # 'step_depends' and 'CONFIG'
    # because they will be included by env.sos_dict['__signature_vars__'] if they are actually
    # used in the task. (issue #752)
    task_vars = env.sos_dict.clone_selected_vars(
        env.sos_dict["__signature_vars__"]
        | {
            "_input",
            "_output",
            "_depends",
            "_index",
            "step_name",
            "_runtime",
            "__signature_vars__",
        }
    )

    task_tags = [
        env.sos_dict["step_name"],
        env.sos_dict["workflow_id"],
        os.path.basename(os.getcwd()),
    ]
    if "tags" in env.sos_dict["_runtime"]:
        if isinstance(env.sos_dict["_runtime"]["tags"], str):
            tags = [env.sos_dict["_runtime"]["tags"]]
        elif isinstance(env.sos_dict["_runtime"]["tags"], Sequence):
            tags = [str(x) for x in list(env.sos_dict["_runtime"]["tags"])]
        else:
            tags = str(env.sos_dict["_runtime"]["tags"]).split(" ")
        #
        for tag in tags:
            if not tag.strip():
                continue
            if not SOS_TAG.match(tag):
                new_tag = re.sub(r"[^\w_.-]", "", tag)
                if new_tag:
                    env.logger.warning(f'Invalid tag "{tag}" is added as "{new_tag}"')
                    task_tags.append(new_tag)
                else:
                    env.logger.warning(f'Invalid tag "{tag}" is ignored')
            else:
                task_tags.append(tag)

    # save task to a file
    taskdef = TaskParams(
        name="{} (index={})".format(env.sos_dict["step_name"], env.sos_dict["_index"]),
        global_def=(global_def, global_vars),
        task=task_stmt,  # task
        sos_dict=task_vars,
        tags=task_tags,
    )
    # if no output (thus no signature)
    # temporarily create task signature to obtain sig_id
    task_id = RuntimeInfo(
        statementMD5([task_stmt]),
        task_vars["_input"],
        task_vars["_output"],
        task_vars["_depends"],
        task_vars["__signature_vars__"],
        task_vars,
    ).sig_id

    # workflow ID should be included but not part of the signature, this is why it is included
    # after task_id is created.
    task_vars["workflow_id"] = env.sos_dict["workflow_id"]
    return task_id, taskdef, task_vars


def kill_all_subprocesses(pid=None, include_self=False):
    # kill all subprocesses that could have been spawn from the current process
    try:
        proc = psutil.Process(pid)
    except Exception:
        # if no such process
        return
    procs = proc.children(recursive=True) + ([proc] if include_self else [])
    if not procs:
        return
    for p in procs:
        p.terminate()
    alive = psutil.wait_procs(procs, timeout=3)[-1]
    if alive:
        for p in alive:
            p.kill()
    alive = psutil.wait_procs(procs, timeout=3)[-1]
    if alive:
        for p in alive:
            env.logger.warning(f"Failed to kill subprocess {p.pid}")


def reevaluate_output():
    # re-process the output statement to determine output files
    args, kwargs = SoS_eval(
        f'__null_func__({env.sos_dict["step_output"]._undetermined})',
        extra_dict={
            "__null_func__": __null_func__,
            "output_from": __output_from__,
            "named_output": __named_output__,
        },
    )
    if args is True:
        env.logger.error("Failed to resolve unspecified output")
        return
    # handle dynamic args
    args = [x.resolve() if isinstance(x, dynamic) else x for x in args]
    kwargs = {
        x: (y.resolve() if isinstance(y, dynamic) else y) for x, y in kwargs.items()
    }
    return sos_targets(*args, **kwargs, _verify_existence=True)


def validate_step_sig(sig):
    if env.config["sig_mode"] in ("default", "skip", "distributed"):
        # if users use sos_run, the "scope" of the step goes beyong names in this step
        # so we cannot save signatures for it.
        matched = sig.validate()
        if isinstance(matched, dict):
            env.logger.info(
                f'``{env.sos_dict["step_name"]}`` (index={env.sos_dict["_index"]}) is ``ignored`` due to saved signature'
            )
            return matched
        else:
            env.logger.debug(f"Signature mismatch: {matched}")
            return {}
    elif env.config["sig_mode"] == "assert":
        matched = sig.validate()
        if isinstance(matched, str):
            raise RuntimeError(f"Signature mismatch: {matched}")
        env.logger.info(
            f'Substep ``{env.sos_dict["step_name"]}`` (index={env.sos_dict["_index"]}) is ``ignored`` with matching signature'
        )
        return matched
    elif env.config["sig_mode"] == "build":
        # build signature require existence of files
        if sig.write():
            env.logger.info(
                f'Step ``{env.sos_dict["step_name"]}`` (index={env.sos_dict["_index"]}) is ``ignored`` with signature constructed'
            )
            return {
                "input": sig.content["input_obj"],
                "output": sig.content["output_obj"],
                "depends": sig.content["depends_obj"],
                "vars": sig.content["end_context"],
            }
    elif env.config["sig_mode"] == "force":
        return {}
    else:
        raise RuntimeError(f'Unrecognized signature mode {env.config["sig_mode"]}')


def strip_param_defs(stmt):
    # the parameters are translated to
    #
    # #begin_parameter name
    # name = sos_get_param("name", value
    # ) #end_parameter name
    #
    # we will need to remove these lines in cases when parameters are not handled
    res = []
    end_line = None
    for line in stmt.splitlines():
        if line.startswith("#begin_parameter "):
            end_line = f") #end_parameter {line[17:]}"
        if not end_line:
            res.append(line)
        elif line == end_line:
            end_line = None
    return "\n".join(res)


def verify_input(ignore_internal_targets=False):
    # now, if we are actually going to run the script, we
    # need to check the input files actually exists, not just the signatures
    for key in ("_input", "_depends"):
        for target in env.sos_dict[key]:
            if not target.target_exists("target") and not (
                ignore_internal_targets and isinstance(target, (sos_variable, sos_step))
            ):
                raise RemovedTarget(target)
