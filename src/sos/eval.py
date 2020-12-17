#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import ast
import sys
import copy
import pickle
import contextlib

from typing import Any, Dict, Optional, Set

from .utils import env, as_fstring, load_config_files, pickleable, ArgumentError
from ._version import __version__


def interpolate(text, global_dict=None, local_dict=None):
    """Evaluate expressions in `text` """
    # step 1, make it a f-string (add quotation marks and f
    # step 2, evaluate as a string
    try:
        return eval(as_fstring(text), global_dict, local_dict)
    except Exception as e:
        raise ValueError(f"Failed to interpolate {text}: {e}")


def cfg_interpolate(text, local_dict={}):
    # handle nested interpolate ...
    # we need to avoid containing our CONFIG with all the modules created by eval
    cfg_dict = copy.deepcopy(env.sos_dict.get("CONFIG", {}))
    if "os.environ" in text:
        exec("import os", cfg_dict)
    while True:
        res = interpolate(text, cfg_dict, local_dict)
        if res == text:
            break
        else:
            text = res
    return res


def get_config(*args, **kwargs):
    """
    Obtain configuration from sos configuration files, by default with variable intepolation.

    The basic way to use this function is to do

       get_config('key')

    which gets the value associated with configuration "key". If the value is a dictionary, you
    can go into the dictionary with any of the following

       get_config('key', 'key1')
       get_config(['key', 'key1'])
       get_config('key.key1')

    The expressions are by default interpolated with variables defined in the order of,

    1. specified from get_config
    2. the same dictionary in which the expression is evaluated
    3. the root of the configuration dictionary

    You can specify variables as a dictionary

        get_config('a', {'val': 5})

    if the key does not conflict with other keywords, you can specify directly as keyword arguments

        get_config('a', val=5)

    The funciton accepts the following keyword parameters:

      default: default value to return if the key is not found. Default to None.
      raw: return raw values without interpolation.
      raw_keys: keys that are not expanded
      expand_keys: keys that are expanded
      allowed_keys: retrieve only specified keys from a dicitonary.
      exclude_keys: exclude specified keys
      expected_type: expected type or types
    """
    default = kwargs.get("default", None)
    allowed_keys = kwargs.get("allowed_keys", None)
    excluded_keys = kwargs.get("excluded_keys", None)
    raw_keys = kwargs.get("raw_keys", None)
    expand_keys = kwargs.get("expand_keys", None)
    raw = kwargs.get("raw", False)
    expected_type = kwargs.get("expected_type", None)

    #
    keys = []
    custom_dict = {}
    for arg in args:
        if isinstance(arg, str):
            keys.append(arg)
        elif isinstance(arg, (list, tuple)):
            keys.extend(list(arg))
        elif isinstance(arg, dict):
            custom_dict.update(arg)
        else:
            raise ValueError(f"Unacceptable parameter {arg} for get_config.")
    #
    custom_dict.update(
        {
            x: y
            for x, y in kwargs.items()
            if x
            not in (
                "default",
                "allowed_keys",
                "excluded_keys",
                "raw_keys",
                "expand_keys",
                "expected_type",
                "raw",
            )
        }
    )
    #
    local_dict = {}
    val = env.sos_dict.get("CONFIG", {})
    while True:
        if not keys:
            break
        if not isinstance(val, dict):
            raise ValueError(
                f'A dictionary is expected to get item with key {".".join(keys)}: {val} obtained.'
            )
        local_dict = val
        key = keys[0]
        if key in val:
            keys.pop(0)
            val = val[key]
            continue
        elif "." in key:
            found = False
            subkeys = key.split(".")
            for j in range(len(subkeys)):
                subkey = ".".join(subkeys[: j + 1])
                if subkey in val:
                    found = True
                    val = val[subkey]
                    keys[0] = ".".join(subkeys[j + 1 :])
                    if not keys[0]:
                        keys.pop(0)
                    break
            if not found:
                val = default
        else:
            val = default

    if expected_type is not None and not isinstance(val, expected_type):
        raise ValueError(
            f'A value of type {expected_type} is expected, "{val}" specified.'
        )

    if raw is True:
        return val

    if isinstance(val, str):
        local_dict.update(custom_dict)
        return cfg_interpolate(val, local_dict)
    elif isinstance(val, dict):

        def interpolate_dict(
            item,
            custom_dict,
            allowed_keys=None,
            excluded_keys=None,
            raw_keys=None,
            expand_keys=None,
        ):
            res = {}
            for k, v in item.items():
                if allowed_keys and k not in allowed_keys:
                    continue
                if excluded_keys and k in excluded_keys:
                    continue
                if raw_keys and k in raw_keys:
                    res[k] = v
                    continue
                if expand_keys and k not in expand_keys:
                    res[k] = v
                    continue
                if isinstance(v, dict):
                    # v should be processed in place
                    res[k] = interpolate_dict(v, custom_dict)
                elif isinstance(v, str):
                    if "{" in v and "}" in v:
                        local_dict = copy.deepcopy(item)
                        local_dict.update(custom_dict)
                        res[k] = cfg_interpolate(v, local_dict)
                    else:
                        res[k] = v
                else:
                    res[k] = v
            return res

        return interpolate_dict(
            val,
            custom_dict,
            allowed_keys=allowed_keys,
            excluded_keys=excluded_keys,
            raw_keys=raw_keys,
            expand_keys=expand_keys,
        )
    else:
        return val


def get_accessed(node):
    """Get names, but ignore variables names to the left hand side
    That is to say, in case of
       a = b + 1
    we consider b as "being accessed", while a is not.
    """
    if isinstance(node, ast.Assign):
        return get_accessed(node.value)
    elif isinstance(node, ast.Name):
        return {node.id}
    names = set()
    if isinstance(node, list):
        for x in node:
            names |= get_accessed(x)
    else:
        for x in ast.iter_child_nodes(node):
            names |= get_accessed(x)
    return names


def accessed_vars(statement: str, mode: str = "exec") -> Set[str]:
    """Parse a Python statement and analyze the symbols used. The result
    will be used to determine what variables a step depends upon."""
    try:
        if mode == "exec":
            return get_accessed(ast.parse(statement, "<string>", "exec"))
        else:
            res = get_accessed(
                ast.parse("__NULL__(" + statement + ")", "<string>", "eval")
            )
            res.remove("__NULL__")
            return res
    except Exception:
        raise RuntimeError(f"Failed to parse statement: {statement} in {mode} mode")


def get_used_in_func(node):
    """Get names, but ignore variables names to the left hand side
    That is to say, in case of
       a = b + 1
    we consider b as "being accessed", while a is not.
    """
    if isinstance(node, ast.FunctionDef):
        return {node.name: get_accessed(node.body)}
    names = {}
    for node in ast.iter_child_nodes(node):
        names.update(get_used_in_func(node))
    return names


def used_in_func(statement: str, filename: str = "<string>", mode: str = "exec"):
    """Parse a Python statement and analyze the symbols used. The result
    will be used to determine what variables a step depends upon."""
    try:
        return get_used_in_func(ast.parse(statement, filename, mode))
    except Exception as e:
        raise RuntimeError(f"Failed to parse statement: {statement} {e}")


def SoS_eval(expr: str, extra_dict: dict = {}) -> Any:
    """Evaluate an expression with sos dict."""
    return eval(expr, env.sos_dict.dict(), extra_dict)


def _is_expr(expr):
    try:
        compile(expr, "<string>", "eval")
        return True
    except Exception:
        return False


class StatementHash(object):
    stmt_hash = {}

    def __init__(self) -> None:
        pass

    def hash(self, script: str) -> str:
        h = hash(script) & sys.maxsize
        StatementHash.stmt_hash[h] = script
        return f"script_{h}"

    def script(self, hash: str) -> str:
        return StatementHash.stmt_hash[int(hash[7:])]


stmtHash = StatementHash()


def SoS_exec(script: str, _dict: dict = None, return_result: bool = True) -> None:
    """Execute a statement."""
    if _dict is None:
        _dict = env.sos_dict.dict()

    if not return_result:
        if env.verbosity == 0:
            with contextlib.redirect_stdout(None):
                exec(
                    compile(script, filename=stmtHash.hash(script), mode="exec"), _dict
                )
        else:
            exec(compile(script, filename=stmtHash.hash(script), mode="exec"), _dict)
        return None

    try:
        stmts = list(ast.iter_child_nodes(ast.parse(script)))
        if not stmts:
            return
        if isinstance(stmts[-1], ast.Expr):
            # the last one is an expression and we will try to return the results
            # so we first execute the previous statements
            if len(stmts) > 1:
                if env.verbosity == 0:
                    with contextlib.redirect_stdout(None):
                        exec(
                            compile(
                                ast.Module(body=stmts[:-1], type_ignores=[]),
                                filename=stmtHash.hash(script),
                                mode="exec",
                            ),
                            _dict,
                        )
                else:
                    exec(
                        compile(
                            ast.Module(body=stmts[:-1], type_ignores=[]),
                            filename=stmtHash.hash(script),
                            mode="exec",
                        ),
                        _dict,
                    )
            # then we eval the last one
            if env.verbosity == 0:
                with contextlib.redirect_stdout(None):
                    res = eval(
                        compile(
                            ast.Expression(body=stmts[-1].value),
                            filename=stmtHash.hash(script),
                            mode="eval",
                        ),
                        _dict,
                    )
            else:
                res = eval(
                    compile(
                        ast.Expression(body=stmts[-1].value),
                        filename=stmtHash.hash(script),
                        mode="eval",
                    ),
                    _dict,
                )
        else:
            # otherwise we just execute the entire code
            if env.verbosity == 0:
                with contextlib.redirect_stdout(None):
                    exec(
                        compile(script, filename=stmtHash.hash(script), mode="exec"),
                        _dict,
                    )
            else:
                exec(
                    compile(script, filename=stmtHash.hash(script), mode="exec"), _dict
                )
            res = None
    except SyntaxError as e:
        raise SyntaxError(f"Invalid code {script}: {e}")

    # if check_readonly:
    #    env.sos_dict.check_readonly_vars()
    return res


#
# dynamic expression that cannot be resolved during parsing
# at prepare mode etc, and has to be resolved at run time.
#


class Undetermined(object):
    def __init__(self, expr: str = "") -> None:
        if not isinstance(expr, str):
            raise RuntimeError(
                f'Undetermined expression has to be a string: "{expr}" passed'
            )
        self.expr = expr.strip()

    def value(self):
        return SoS_eval(self.expr)

    def __repr__(self) -> str:
        return f"Undetermined({self.expr!r})"

    def __hash__(self):
        raise RuntimeError(
            "Undetermined expression should be evaluated before used. "
            "This is certainly a bug so please report this to SoS developer."
        )

    def targets(self) -> "Undetermined":
        return self


class on_demand_options(object):
    """Expression that will be evaluated upon request."""

    def __init__(self, items: Optional[Dict[str, Any]]) -> None:
        self._expressions = {}
        if items:
            self._expressions.update(items)

    def set(self, key: str, value: Any) -> None:
        self._expressions[key] = repr(value)

    def __contains__(self, key: str) -> bool:
        return key in self._expressions

    def __setitem__(self, key: str, value: str) -> None:
        self._expressions[key] = value

    def __getitem__(self, key: str) -> Any:
        # first check if the value if cached
        if key not in self._expressions:
            raise KeyError(key)
        try:
            return SoS_eval(self._expressions[key])
        except Exception as e:
            if key == "skip":
                raise ValueError(
                    f"Failed to evaluate option {key} with value {self._expressions[key]}: Only constant values are allowed for section option skip"
                )
            else:
                raise ValueError(
                    f"Failed to evaluate option {key} with value {self._expressions[key]}: {e}"
                )

    def __repr__(self):
        return repr(self._expressions)


class KeepOnlyImportAndDefine(ast.NodeTransformer):
    def __init__(self):
        self.level = 0

    def generic_visit(self, node):
        self.level += 1
        if self.level == 2 and not isinstance(
            node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)
        ):
            # print(f'remove {node}')
            ret = None
        else:
            ret = super(KeepOnlyImportAndDefine, self).generic_visit(node)
        self.level -= 1
        return ret


def analyze_global_statements(global_stmt):
    # find all import and function definition ...
    env.sos_dict.clear()
    env.sos_dict.set("SOS_VERSION", __version__)
    env.sos_dict.set("master_id", "")
    env.sos_dict.set("workflow_id", "")
    env.sos_dict.set("step_name", "")
    env.sos_dict.set("step_id", "")
    # first load CONFIG, this will create CONFIG
    load_config_files()

    # run only import, def, and class of the global_def
    transformer = KeepOnlyImportAndDefine()
    global_def = transformer.visit(
        ast.parse("from sos.runtime import *\n" + global_stmt)
    )
    exec(compile(global_def, filename="<ast>", mode="exec"), env.sos_dict._dict)
    defined_keys = set(env.sos_dict.keys())

    # execute the entire statement
    try:
        SoS_exec(global_stmt)
    except ArgumentError:
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to execute global statement: {e}")
    #
    global_vars = {
        k: env.sos_dict[k]
        for k in (set(env.sos_dict.keys()) - defined_keys) | {"SOS_VERSION", "CONFIG"}
    }
    # test if global vars can be pickled
    try:
        pickle.dumps(global_vars)
    except Exception:
        for key in set(env.sos_dict.keys()) - defined_keys:
            if not pickleable(env.sos_dict[key], key):
                raise ValueError(
                    f"Variable {key} cannot be defined in global section because it cannot be pickled to workers."
                )
    return global_def, global_vars
