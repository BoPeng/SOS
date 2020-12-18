#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import ast
import builtins
import copy
import fnmatch
import os
import re
import shutil
import sys
import textwrap
from io import StringIO, TextIOBase
from tokenize import generate_tokens
import typing
from typing import Dict, List, Optional, Tuple, Any, Set
from uuid import UUID, uuid4

from .eval import on_demand_options
from .syntax import (
    INDENTED,
    SOS_CELL,
    SOS_DIRECTIVE,
    SOS_DIRECTIVES,
    SOS_FORMAT_LINE,
    SOS_FORMAT_VERSION,
    SOS_INDENTED_ACTION,
    SOS_MAGIC,
    SOS_SECTION_HEADER,
    SOS_SECTION_NAME,
    SOS_SECTION_OPTION,
    SOS_SUBWORKFLOW,
    SOS_ACTION_OPTIONS,
)
from .targets import file_target, path, paths, sos_targets
from .utils import (
    Error,
    env,
    locate_script,
    as_fstring,
    valid_expr_till,
    format_par,
    separate_options,
    textMD5,
)

__all__ = ["SoS_Script"]

# these are needed by eval as recognizable types
assert file_target
assert path
assert paths
assert sos_targets


class ParsingError(Error):
    """Raised when a configuration file does not follow legal syntax."""

    def __init__(self, filename: str) -> None:
        Error.__init__(
            self,
            f'File contains parsing errors: {filename if filename != "<string>" else ""}',
        )
        self.filename = filename
        self.errors: List[Tuple[int, str]] = []
        self.args = (filename,)

    def append(self, lineno: int, line: str, msg: str) -> None:
        if (lineno, line) in self.errors:
            return
        self.errors.append((lineno, line))
        self.message += f"\n\t[line {lineno:2d}]: {line}\n{msg}"


_action_list = None


def is_type_hint(stmt: str) -> bool:
    """Try to differentiate

    var: type = value

    with

    action: input = whatever

    """
    if stmt.count("=") > 1:
        return False

    if ":" not in stmt:
        return False

    #
    # action:
    if not stmt.split(":")[1].strip():
        return False

    #
    # action: int
    #
    # or
    #
    # input: variable
    #
    if "=" not in stmt:
        action, par = [x.strip() for x in stmt.split(":", 1)]
    else:
        # one parameter?
        #
        # action: input={'a': b}
        #
        action, par = [x.strip() for x in stmt.split("=", 1)[0].split(":", 1)]

    if action in SOS_DIRECTIVES:
        return False
    if par in SOS_ACTION_OPTIONS:
        return False

    # if par is something like List[Any], or 'classname'
    if not par.isidentifier():
        return True

    # if action is a builtin function, such as sort, it cannot be
    # a variable assignment.
    if action in dir(builtins):
        return False

    # if action is registered
    global _action_list
    if _action_list is None:
        import pkg_resources

        _action_list = [
            x.name for x in pkg_resources.iter_entry_points(group="sos_actions")
        ]
    if action in _action_list:
        return False

    # if par is something like List, Tuple, str
    if par in dir(typing) or par in dir(builtins):
        return True

    # if not quite sure???
    env.logger.debug(
        f"Failed to tell if '{stmt}' is an assignment with type hint or function in script format. Assuming type hint."
    )
    # regular function written in this format?
    return True


def extract_option_from_arg_list(
    options: str, optname: str, default_value: None
) -> Tuple[Any, str]:
    if not options:
        return default_value, options
    try:
        args = list(ast.iter_fields(ast.parse(f"f({options})", mode="eval")))[0][
            1
        ].keywords
        for field in args:
            if field.arg == optname:
                try:
                    value = eval(
                        compile(
                            ast.Expression(body=field.value),
                            filename="<ast>",
                            mode="eval",
                        )
                    )
                    new_options = ",".join(
                        [
                            x
                            for x in options.split(",")
                            if not x.strip().startswith(optname)
                        ]
                    )
                    return value, new_options.strip()
                except Exception:
                    raise ValueError(
                        f"A constant value is expected for option {optname}: {options} provided."
                    )
        return default_value, options
    except SyntaxError:
        raise ValueError(f"Expect a list of keyword arguments: {options} provided")


def replace_sigil(text: str, sigil: str) -> str:
    if sigil == "{ }":
        return text
    if sigil is not None and (
        sigil.count(" ") != 1
        or sigil[0] in (" ", "'")
        or sigil[-1] in (" ", "'")
        or sigil.split(" ")[0] == sigil.split(" ")[1]
    ):
        raise ValueError(f'Incorrect sigil "{sigil}"')
    # then we need to replace left sigil as { and right sigil asn }
    l, r = sigil.split(" ")
    # now that we have the correct sigil
    # first, we need to replace all { as {{ and } as }}
    lp = re.compile(re.escape(l))
    rp = re.compile(re.escape(r))
    final_text = ""
    while True:
        pieces = lp.split(text, 1)
        if len(pieces) == 1:
            # cannot split
            final_text += text.replace("{", "{{").replace("}", "}}")
            break
        else:
            rhs = pieces[1]
            # now if there is {, we need to find matching number of }
            # basically we need to find syntaxly valid expression before
            # ending bracket... or !
            rhs_pieces = rp.split(rhs)
            if len(rhs_pieces) == 1:
                raise ValueError(
                    f"Invalid f-string {text}: missing right sigil at {rhs[:20]}"
                )

            # say we have sigil = [ ]
            #
            # rhs = 'whatever ]' :r] something else [ ]
            #
            # we need to include ] in expression
            #
            for n in range(1, len(rhs_pieces) + 1):
                if valid_expr_till(r.join(rhs_pieces[:n])) > 0:
                    final_text += (
                        pieces[0].replace("{", "{{").replace("}", "}}")
                        + "{"
                        + r.join(rhs_pieces[:n])
                        + "}"
                    )
                    text = r.join(rhs_pieces[n:])
                    break
                # the last one, still not valid
                if n == len(rhs_pieces):
                    raise ValueError(f"Invalid f-string {text}: invalid expression")
    # finally, replace LSIGIL etc
    return final_text


def get_names_of_kwargs(param_list):
    tree = ast.parse(f"__null_func__({param_list})")
    return [x.arg for x in tree.body[0].value.keywords]


class SoS_Step:
    """Parser of a SoS step. This class accepts strings sent by the parser, determine
    their types and add them to appropriate sections (directive, statement,
    scripts etc)"""

    def __init__(
        self,
        context: Optional["SoS_ScriptContent"] = None,
        names=None,
        options=None,
        is_global: bool = False,
        comment: str = "",
    ) -> None:
        """A sos step """
        self.context = context
        # A step will not have a name and index until it is copied to separate workflows
        self.name = None
        self.index = None
        self.alias = None
        # it initially hold multiple names with/without wildcard characters
        self.names = [] if names is None else names
        # everything before step process
        self.statements: List = []
        self.global_parameters: Dict = {}
        self.parameters: Dict = {}
        self.substep_parameters: Set = set()
        # step processes
        self.global_stmts = ""
        self.global_def = ""
        self.global_vars = {}
        self.task = ""
        self.task_params = ""
        self.last_step = None
        self.comment = comment
        # is it global section? This is a temporary indicator because the global section
        # will be inserted to each step of the workflow.
        self.is_global = is_global
        # indicate the type of input of the last line
        self.values: List = []
        self.lineno = 0
        #
        self.runtime_options: Dict = {}
        self.options = on_demand_options(options)
        #
        # string mode to collect all strings as part of an action
        self._action = None
        self._action_options = ""
        self._script = ""

    def has_external_task(self) -> bool:
        return self.task != ""

    # def has_nested_workflow(self) -> bool:
    #     return any('sos_run' in x[1] for x in self.statements)

    def step_name(self, alias: bool = False) -> str:
        # if the step is not part of any workflow and is not given a name
        if self.name is None:
            names = []
            for n, i, a in self.names:
                if alias and a:
                    names.append(a)
                elif n and i is not None:
                    names.append(f"{n}_{i}")
                else:
                    names.append(n if n is not None else str(i))
            return ", ".join(names)
        else:
            if alias and self.alias:
                return self.alias
            elif self.name and self.index is not None:
                return f"{self.name}_{self.index}"
            else:
                return self.name if self.name else str(self.index)

    def match(self, step_name: str) -> bool:
        # if this step provides name...
        for name, index, _ in self.names:
            if (
                step_name == name
                or step_name == f"{name}_{0 if index is None else int(index)}"
            ):
                return True
        return False

    def indented_script(self) -> bool:
        """ check self._script and see if it is indented """
        # get all leading space, tab and newline
        leading = INDENTED.match(self._script)
        return 0 if leading is None else len(leading.group(2))

    def category(self) -> Optional[str]:
        """Determine the category of existing statement"""
        if self.statements:
            if self.statements[-1][0] == ":":
                # a hack. ... to avoid calling isValid recursively
                def validDirective():
                    if not self.values:
                        return True
                    if self.values[-1].strip().endswith(","):
                        return False
                    try:
                        compile(
                            "func(" + "".join(self.values) + ")",
                            filename="<string>",
                            mode="eval",
                        )
                    except Exception:
                        return False
                    return True

                if validDirective() and self._action is not None:
                    return "script"
                return "directive"
            return "statements"
        return None

    def isValid(self) -> bool:
        """Determine if the statement, expression or directive is valid. Otherwise
        the parser will continue until a valid multi-line expression or statement
        can be found."""
        if not self.values:
            return True
        try:
            if self.category() == "directive":
                # we add func() because the expression can be multi-line and
                # can have keyword-argument like options
                #
                # However, python considers
                #
                #     func('value', )
                #
                # a valid syntax but we do want , to continue to the next line
                if self.values[-1].strip().endswith(","):
                    self.error_msg = "Trailing ,"
                    return False
                # to allow type trait, we will have to test the expression as if in a function
                # definition, with something like "def func(a : str, b : list=[])"
                try:
                    compile(
                        "func(" + "".join(self.values) + ")",
                        filename="<string>",
                        mode="eval",
                    )
                except Exception:
                    compile(
                        "def func(" + "".join(self.values) + "):\n  pass",
                        filename="<string>",
                        mode="exec",
                    )
            elif self.category() == "statements":
                compile(("".join(self.values)), filename="<string>", mode="exec")
            elif self.category() == "script":
                #
                # A valid script has an identation defined at the first line. That is to say
                #
                # line 1
                # line 2
                #
                # is allowed
                #
                #     line 1
                #     line 2
                #
                # line 3
                #
                # is not so the addition of line 3 would fail. However, the last line
                # will be tested before inserted so this function will always return True
                return True
            else:
                raise RuntimeError(f"Unrecognized expression type {self.category()}")
            return True
        except Exception as e:
            self.error_msg = repr(e)
            return False

    def empty(self) -> bool:
        """If there is no content (comment does not count)"""
        return self.category() is None

    def extend(self, line: str) -> None:
        """Extend the current directive, expression or script"""
        if self.category() == "directive":
            self.add_directive(None, line)
        elif self.category() == "script":
            self._script += line
        else:
            self.add_statement(line)

    def add_directive(
        self,
        key: Optional[str],
        value: str,
        lineno: Optional[int] = None,
        comment: str = "",
    ) -> None:
        """Assignments are items with ':' type """
        if key is None:
            # continuation of multi-line directive
            self.statements[-1][2] += value
            self.values.append(value)
            if self._action is not None:
                self._action_options += value
        else:
            # new directive, the comment before it are used
            self.statements.append([":", key, value, comment])
            self.values = [value]
        if lineno:
            self.lineno = lineno

    def add_script(self, key: str, value: str, lineno: Optional[int] = None) -> None:
        """script starts with key: value"""
        # we need a fake directive here because the : directive can be multi-line and
        # we need to borrow the syntax checking of directives here.
        self.statements.append([":", "__script__", ""])
        self.values = [value]
        self._action = key
        self._action_options = value
        if lineno:
            self.lineno = lineno

    def add_statement(self, line: str, lineno: Optional[int] = None) -> None:
        """statements are regular python statements"""
        # there can be only one statement block
        if self.category() != "statements":
            self.values = [line]
        else:
            self.values.append(line)
        if self.statements and self.statements[-1][0] == "!":
            self.statements[-1][-1] += line
        else:
            self.statements.append(["!", line])
        if lineno:
            self.lineno = lineno

    def wrap_script(self) -> None:
        """convert action: script to task: action(script)"""
        if self._action is None:
            return
        # _action options can contain both runtime option and action options
        opt = self._action_options.strip()
        if self.statements[-1][0] != ":" or self.statements[-1][1] != "__script__":
            raise RuntimeError("Failed to parse script")
        # under window, the lines will be ended with \r\n, which will cause
        # trouble with textwrap.dedent.
        embedded_script = "\n".join(self._script.splitlines()) + "\n"
        # dedent, which will not remove empty line, so _scrit will always ends with \n.
        self._script = textwrap.dedent(embedded_script)
        if self._script == embedded_script:
            first_words = self._script[:10].splitlines()[0]
            env.logger.warning(
                f'Embedding script "{first_words}..." without indentation is error-prone and will be deprecated in the future.'
            )
        # let us look for 'expand=""' in options
        if "expand" in opt:
            sigil, opt = extract_option_from_arg_list(opt, "expand", None)
            if sigil is None or sigil is False:
                # no expansion
                self._script = repr(self._script)
            elif sigil is True:
                self._script = as_fstring(self._script)
            else:
                self._script = as_fstring(replace_sigil(self._script, sigil))
        else:
            # verbatim script, use repr is enough
            self._script = repr(self._script)
        self.statements[-1] = [
            "!",
            f'{self._action}({self._script}{(", " + opt) if opt else ""})\n',
        ]
        self.values = []
        self._action = None
        self._action_options = None
        self._script = ""

    def get_tokens(self) -> str:
        """Get tokens after input statement"""

        def _get_tokens(statement):
            return [
                x[1]
                for x in generate_tokens(StringIO(statement).readline)
                if x[1] not in ("", "\n")
            ]

        tokens: List = []
        for statement in self.statements:
            tokens.extend(
                _get_tokens(statement[2] if statement[0] == ":" else statement[1])
            )

        if self.task:
            tokens.extend(_get_tokens(self.task))

        return " ".join(tokens)

    def finalize(self) -> None:
        """ split statement and task by last directive """
        self.wrap_script()
        if not self.statements:
            self.task = ""
            return
        # handle tasks
        input_directive = [
            idx
            for idx, statement in enumerate(self.statements)
            if statement[0] == ":" and statement[1] == "input"
        ]
        task_directive = [
            idx
            for idx, statement in enumerate(self.statements)
            if statement[0] == ":" and statement[1] == "task"
        ]
        if len(task_directive) > 1:
            raise ValueError("Only one task statement is allowed in a step")
        # handle parameter
        for idx, statement in enumerate(self.statements):
            if statement[0] == ":" and statement[1] == "parameter":
                if task_directive and task_directive[0] < idx:
                    raise ValueError("Parameter statement is not allowed in tasks.")
                if "=" not in statement[2]:
                    if ":" in statement[2]:
                        if not is_type_hint(statement[2]):
                            raise ValueError(
                                f"Invalid type trait in parameter specification {statement[2]}"
                            )
                        name, value = statement[2].split(":")
                    else:
                        name = statement[2]
                        value = "str"
                else:
                    name, value = statement[2].split("=", 1)
                    # ignore type trait if a default value is specified
                    name = name.split(":")[0]
                name = name.strip()
                if name.startswith("_"):
                    raise ValueError(
                        f"Invalid parameter name {name}: names with leading underscore is not allowed."
                    )
                if not value.strip():
                    raise ValueError(
                        f"{self.step_name()}: Invalid parameter definition: {statement[2]}"
                    )
                # there is a possibility that value contains # so  sos_handle_parameter(name, val # aaa) will fail
                self.statements[idx] = [
                    "!",
                    f"#begin_parameter {name}\n{name} = sos_get_param({name.strip()!r}, {value}\n) #end_parameter {name}\n",
                    statement[2].strip(),
                ]
                self.parameters[name] = (value, statement[3])
                if input_directive and input_directive[0] < idx:
                    self.substep_parameters.add(name)
        # handle tasks
        if not task_directive:
            self.task = ""
        else:
            start_task = task_directive[0] + 1
            # convert statement to task
            self.task = ""
            for statement in self.statements[start_task:]:
                if statement[0] == ":":
                    if statement[1] in ("input", "output", "depends"):
                        raise ValueError(
                            f"{self.step_name()}: Step task should be defined as the last item in a SoS step"
                        )
                    elif statement[1] == "task":
                        raise ValueError(
                            f"{self.step_name()}: Only one task is allowed for a step"
                        )
                    elif statement[1] == "parameter":
                        raise ValueError(
                            f"{self.step_name()}: Parameters should be defined before step task"
                        )
                    # ignore ...
                    self.task += "\n"
                else:
                    self.task += statement[1]
            self.task_params = self.statements[task_directive[0]][2]
            self.statements = self.statements[: task_directive[0]]
        # merge multiple statments at the end
        if len(self.statements) > 1 and self.statements[-1][0] == "!":
            starting = len(self.statements) - 1
            for idx in range(starting - 1, -1, -1):
                if self.statements[idx][0] == "!":
                    starting = idx
                else:
                    break
            # merge
            for idx in range(starting + 1, len(self.statements)):
                self.statements[starting][1] += self.statements[idx][1]
            # remove the rest of the statements
            self.statements = self.statements[: starting + 1]
        #
        # auto provides #859
        if (
            not any(opt in self.options for opt in ("provides", "shared"))
            and len([x for x in self.statements if x[0] == ":" and x[1] == "output"])
            == 1
        ):
            output_stmt = [
                x for x in self.statements if x[0] == ":" and x[1] == "output"
            ][0][2]
            output_names = get_names_of_kwargs(output_stmt)
            self.options["namedprovides"] = repr(output_names)

    def show(self):
        """Output for command sos show"""
        textWidth = max(60, shutil.get_terminal_size((80, 20)).columns)
        text = f'  {self.step_name() + ":":<21} ' + self.comment
        print(
            "\n".join(
                textwrap.wrap(
                    text, width=textWidth, initial_indent="", subsequent_indent=" " * 24
                )
            )
        )
        local_parameters = {
            x: y for x, y in self.parameters.items() if x not in self.global_parameters
        }
        if local_parameters:
            print("    Workflow Options:")
        for name, (value, comment) in local_parameters.items():
            par_str = f"      {format_par(name, value)}"
            print(par_str)
            if comment:
                print(
                    "\n".join(
                        textwrap.wrap(
                            comment,
                            width=textWidth,
                            initial_indent=" " * 24,
                            subsequent_indent=" " * 24,
                        )
                    )
                )


class SoS_Workflow:
    """A SoS workflow with multiple steps. It is created from multiple sections of a SoS script
    and consists of multiple SoS_Step."""

    def __init__(
        self,
        content: "SoS_ScriptContent",
        workflow_name: str,
        allowed_steps: Optional[str],
        sections: List[SoS_Step],
        global_stmts: str,
    ) -> None:
        """create a workflow from its name and a list of SoS_Sections (using name matching)"""
        self.content = content
        self.name = workflow_name if workflow_name else "default"
        self.sections: List = []
        self.auxiliary_sections: List = []
        self.global_stmts = global_stmts
        #
        for section in sections:
            for name, index, alias in section.names:
                self.auxiliary_sections.append(section)
                self.auxiliary_sections[-1].name = name
                self.auxiliary_sections[-1].index = index
                self.auxiliary_sections[-1].alias = alias
                self.auxiliary_sections[-1].uuid = uuid4()
                # an auxiliary step can also serve as a regular step
                # as long as it matches workflow name.
                if (name == "" and workflow_name == "default") or fnmatch.fnmatch(
                    workflow_name, name
                ):
                    self.sections.append(copy.deepcopy(section))
                    self.sections[-1].name = (
                        name
                        if name == "" and workflow_name == "default"
                        else workflow_name
                    )
                    self.sections[-1].index = index
                    self.sections[-1].alias = alias
                    self.sections[-1].uuid = uuid4()
        #
        # sort sections by index
        self.sections.sort(key=lambda x: 0 if x.index is None else x.index)
        #
        # disable some disallowed steps
        if allowed_steps:
            all_steps = {
                x.index: False for x in self.sections if x.index and x.index >= 0
            }
            #
            for item in allowed_steps.split(","):
                # remove space
                item = "".join([x for x in item if x != " "])
                if item.isdigit():
                    # pipeline:100
                    all_steps[int(item)] = True
                elif "-" in item and item.count("-") == 1:
                    l, u = item.split("-")
                    if (
                        (l and not l.isdigit())
                        or (u and not u.isdigit())
                        or (l and u and int(l) > int(u))
                    ):
                        raise ValueError(f"Invalid pipeline step item {item}")
                    # pipeline:-100, pipeline:100+ or pipeline:10-100
                    if not l:
                        l = min(all_steps.keys())
                    if not u:
                        u = max(all_steps.keys())
                    #
                    for key in all_steps.keys():
                        if key >= int(l) and key <= int(u):
                            all_steps[key] = True
                else:
                    raise ValueError(f"Invalid pipeline step item {item}")
            # keep only selected steps (and the global section)
            self.sections = [
                x for x in self.sections if x.index < 0 or all_steps[x.index]
            ]
        # assign 'last_step' to each section
        last_step = None
        for section in self.sections:
            section.last_step = last_step
            last_step = section.step_name()
        # for auxiliary steps they may also have the step -1
        self.auxiliary_sections.sort(
            key=lambda x: (x.name, 0 if x.index is None else x.index)
        )
        for idx, section in enumerate(self.auxiliary_sections):
            if idx == 0 or section.index is None:
                continue
            if self.auxiliary_sections[idx - 1].name == section.name:
                section.last_step = self.auxiliary_sections[idx - 1].step_name()

    def section_by_id(self, uuid: UUID) -> SoS_Step:
        for section in self.sections:
            if uuid == section.uuid:
                return section
        for section in self.auxiliary_sections:
            if uuid == section.uuid:
                return section
        raise RuntimeError(f"Failed to find section with uuid {uuid}")

    def extend(self, workflow: "SoS_Workflow") -> None:
        """Append another workflow to existing one to created a combined workflow"""
        # all sections are simply appended ...
        # but we will need to make sure that the new workflow is
        # executed after the previous one.
        if not workflow.sections:
            return
        if not self.sections:
            self.sections = workflow.sections
            return
        section = workflow.sections[0]
        depends_idx = [
            idx
            for idx, stmt in enumerate(section.statements)
            if stmt[0] == ":" and stmt[1] == "depends"
        ]
        if not depends_idx:
            section.statements.insert(
                0, [":", "depends", f"sos_step('{self.sections[-1].step_name()}')"]
            )
        else:
            section.statements[depends_idx[0]][2] = (
                section.statements[depends_idx[0]][2].strip()
                + (", " if section.statements[depends_idx[0]][2].strip() else "")
                + f"sos_step('{self.sections[-1].step_name()}')\n"
            )
        self.sections.extend(workflow.sections)

    def has_external_task(self) -> bool:
        return any(x.has_external_task() for x in self.sections) or any(
            x.has_external_task() for x in self.auxiliary_sections
        )

    def parameters(self) -> Dict[str, str]:
        # collect parameters defined by `parameter:` of steps
        par: Dict = {}
        for x in self.sections + self.auxiliary_sections:
            par.update(x.parameters)
        return {x: y[0] for x, y in par.items()}


class SoS_ScriptContent:
    """A small class to record the script information to be used by nested
    workflow."""

    def __init__(self, content: str = "", filename: Optional[str] = None) -> None:
        self.content = content
        self.filename = filename
        self.included = []
        self.md5 = self.calc_md5()

    def calc_md5(self) -> str:
        cnt = self.text()
        # additional files
        for script in self.included:
            cnt += script[0]
        #
        return textMD5(cnt)

    def text(self) -> str:
        if self.content:
            return self.content
        else:
            with open(self.filename) as script:
                return script.read()

    def __repr__(self):
        return f"{self.md5}: filename: {self.filename}, content: {self.content}"

    def __eq__(self, other):
        return self.md5 == other.md5

    def __ne__(self, other):
        return self.md5 != other.md5


class SoS_Script:
    def __init__(
        self, content: Optional[str] = "", filename: Optional[str] = None
    ) -> None:
        """Parse a sectioned SoS script file. Please refer to the SoS manual
        for detailed specification of this format.

        Parameter `content` can be either a filename or a content of a
        SoS script in unicode, which is convenient for passing scripts for
        testing purposes.

        Parameter `filename` should be used if the content should be read
        from a file.

        A SoS_Script generated from a .sos file contains the following items

        sections:
            A list of SoS_Step objects that present all sections read.
            Among other things, section.names
        """
        if not content:
            try:
                content, self.sos_script = locate_script(filename, start=".")
            except Exception:
                # try to add .sos extension?
                if not filename.endswith(".sos"):
                    try:
                        content, self.sos_script = locate_script(
                            filename + ".sos", start="."
                        )
                    except Exception:
                        if not filename.endswith(".ipynb"):
                            try:
                                content, self.sos_script = locate_script(
                                    filename + ".ipynb", start="."
                                )
                            except Exception as e:
                                env.logger.debug(e)
                                env.logger.error(
                                    f"Failed to locate {filename}, {filename}.sos, or {filename}.ipynb"
                                )
                                sys.exit(1)
                        else:
                            raise
                else:
                    raise
            # Is this script in sos or ipynb format?
            ext = os.path.splitext(self.sos_script)[-1]
            if ext == ".ipynb":
                # convert ipynb to sos
                from .converter import extract_workflow

                content = extract_workflow(self.sos_script)
                self.sos_script = "<string>"
                self.content = SoS_ScriptContent(content, None)
            else:
                self.content = SoS_ScriptContent(content, self.sos_script)
        else:
            self.sos_script = "<string>"
            self.content = SoS_ScriptContent(content, None)
        # save a parsed version of the script for displaying purpose only
        self.global_stmts = ""

        self.description = []
        self._last_comment = ""
        # open the file
        if content:
            with StringIO(content) as fp:
                self._read(fp)
        else:
            with open(self.sos_script) as fp:
                self._read(fp)
        #
        # workflows in this script, from sections that are not skipped.
        all_section_steps = sum([x.names for x in self.sections], [])
        forward_section_steps = sum(
            [x.names for x in self.sections if "provides" not in x.options], []
        )
        # (name, None) is auxiliary steps
        self.workflows = list(
            dict.fromkeys([x[0] for x in all_section_steps if "*" not in x[0]])
        )
        forward_workflows = list(
            dict.fromkeys([x[0] for x in forward_section_steps if "*" not in x[0]])
        )
        if not forward_workflows:
            self.workflows.append("default")
            self.default_workflow = "default"
        elif len(forward_workflows) == 1:
            self.default_workflow = forward_workflows[0]
        else:
            self.default_workflow = None
        #
        # now we need to record the workflows to the global section so
        # that we know if which has to be included when a subworkflow is used.
        for section in self.sections:
            if section.is_global:
                section.names = self.workflows

    def _find_include_file(self, sos_file: str) -> Tuple[str, str]:
        # we could almost use SoS_script directly but we need to be able to start searching
        # from path of the master file.
        try:
            if self.sos_script and self.sos_script != "<string>":
                start_path = os.path.split(self.sos_script)[0]
            else:
                start_path = ""
            try:
                content, script_file = locate_script(
                    sos_file + ".sos", start=start_path
                )
            except Exception:
                content, script_file = locate_script(
                    sos_file + ".ipynb", start=start_path
                )
                # convert ipynb to sos
                from .converter import extract_workflow

                content = extract_workflow(script_file)
        except Exception:
            raise RuntimeError(
                f"Source file for nested workflow {sos_file} with extension .sos or .ipynb does not exist"
            )

        return content, script_file

    def add_comment(self, line: str) -> None:
        """Keeping track of "last comment" for section and parameter """
        # the rule is like
        #
        # # comment line  --> add to last comment
        # blank line --> clears last comment
        # [ ] --> use last comment
        # parameter: --> use last comment
        # All others: clear last comment
        self._last_comment += (" " if self._last_comment else "") + line.lstrip(
            "#"
        ).strip()

    def clear_comment(self):
        self._last_comment = ""

    def _read(self, fp: TextIOBase) -> None:
        self.sections: List = []
        self.format_version: str = "1.0"
        self.gloal_def: str = ""
        #
        comment_block = 1
        # cursect always point to the last section
        cursect = None
        all_step_names = []
        #
        # this ParsingError is a container for all parsing errors. It will be
        # raised after parsing if there is at least one parsing error.
        parsing_errors = ParsingError(self.sos_script)
        for lineno, line in enumerate(fp, start=1):
            #
            # for structural lines
            if SOS_MAGIC.match(line):
                continue

            if SOS_CELL.match(line):
                continue

            # comments in SoS scripts are mostly informative
            if line.startswith("#"):
                # Comment blocks before any section
                self.add_comment(line)
                if cursect is None:
                    if comment_block == 1:
                        # look for format information
                        mo = SOS_FORMAT_LINE.match(line)
                        if mo:
                            format_name = mo.group("format_name")
                            if not format_name.upper().startswith("SOS"):
                                parsing_errors.append(
                                    lineno,
                                    line,
                                    f"Unrecognized file format name {format_name}. Expecting SOS.",
                                )
                            mo = SOS_FORMAT_VERSION.match(format_name)
                            if mo:
                                self.format_version = mo.group("format_version")
                            else:
                                parsing_errors.append(
                                    lineno,
                                    line,
                                    f"Unrecognized file format version in {format_name}.",
                                )
                    elif comment_block == 2:
                        # anything before the first section can be pipeline
                        # description.
                        if cursect is None:
                            self.description.append(line)
                else:
                    # this is description of the section
                    if cursect.empty():
                        pass
                    # this is comment in scripts (and perhaps not even comment)
                    elif cursect.category() == "script":
                        if cursect.indented_script():
                            # if the script is indented and encounters a comment
                            # from first column, switch to comment mode
                            try:
                                cursect.wrap_script()
                            except Exception as e:
                                parsing_errors.append(lineno, line, str(e))
                        else:
                            cursect.extend(line)
                    # this can be comment or back comment
                    elif cursect.category() == "statements":
                        if not cursect.isValid():
                            cursect.extend(line)
                continue
            elif not line.strip():
                # a blank line start a new comment block if we are still
                # in the front of the script
                self.clear_comment()
                if cursect is None:
                    comment_block += 1
                else:
                    category = cursect.category()
                    if category in ("statements", "script"):
                        cursect.extend(line)
                    elif category == "directive" and not cursect.isValid():
                        # 1311
                        parsing_errors.append(
                            lineno,
                            line,
                            f'Ending incomplete options ({"".join(cursect.values).strip()}) with an empty line',
                        )
                continue

            #
            # a continuation of previous item?
            if line[0].isspace() and cursect is not None and not cursect.empty():
                mo = SOS_INDENTED_ACTION.match(line)

                if mo:
                    #
                    # case 0:
                    #
                    # [1]
                    #    python: whatever
                    #
                    if not cursect:
                        parsing_errors.append(
                            cursect.lineno,
                            "".join(cursect.values[:5]),
                            "Section cannot start from indented action.",
                        )
                        continue

                    # case 1:
                    #
                    # [1]
                    # if True:
                    #    report:
                    #        something
                    #    whatever: <- this line
                    if cursect.category() == "script":
                        if cursect.indented_script() > re.search(r"\S", line).start():
                            try:
                                cursect.wrap_script()
                            except Exception as e:
                                parsing_errors.append(
                                    cursect.lineno, "".join(cursect.values[:5]), str(e)
                                )
                        else:
                            # case 2:
                            #
                            # if True:
                            #     report:
                            #        name: whatever
                            cursect.extend(line)
                            continue

                    # not script, or a the script has been wrapped
                    action_name = mo.group("action_name")
                    # newline should be kept in case of multi-line directive
                    action_value = mo.group("action_value") + "\n"
                    cursect.add_script(action_name, action_value, lineno)
                elif cursect.indented_script() > re.search(r"\S", line).start():
                    # case of wrapping previous script with NO indented action
                    #
                    # if True:
                    #     sh:
                    #        has indent
                    #     this line <-  NOT match
                    # or this line <- NOT match
                    try:
                        cursect.wrap_script()
                    except Exception as e:
                        parsing_errors.append(lineno, line, str(e))
                    cursect.extend(line)
                else:
                    # other cases
                    #
                    cursect.extend(line)
                continue
            #
            # is it a continuation of uncompleted directive?
            if cursect and not cursect.isValid():
                cursect.extend(line)
                continue
            #
            # a new line (start from first column)
            #
            # section header?
            mo = SOS_SECTION_HEADER.match(line)
            if mo:
                # check previous expression before a new section
                if cursect:
                    if not cursect.isValid():
                        parsing_errors.append(
                            cursect.lineno,
                            "".join(cursect.values[:5]),
                            f"Invalid {cursect.category()}: {cursect.error_msg}",
                        )
                    cursect.values = []
                    try:
                        cursect.finalize()
                    except Exception as e:
                        parsing_errors.append(
                            cursect.lineno, "".join(cursect.values[:5]), str(e)
                        )
                # start a new section
                section_name = mo.group("section_name").strip()
                section_option = mo.group("section_option")
                step_names = []
                step_options = {}
                #
                for name in section_name.split(","):
                    mo = SOS_SECTION_NAME.match(name)
                    if mo:
                        n, i, di, al = mo.group(
                            "name", "index", "default_index", "alias"
                        )
                        if n == "global" and i is not None:
                            parsing_errors.append(
                                lineno, line, "Invalid global section definition"
                            )
                        if n == "":
                            parsing_errors.append(
                                lineno, line, "Empty step name is not allowed"
                            )
                        if i and str(int(i)) != i:
                            # disallow cases such as a_01
                            parsing_errors.append(
                                lineno,
                                line,
                                f"Invalid section index {i} (leading zero is not allowed)",
                            )
                        if n:
                            if i is None and "*" in n:
                                parsing_errors.append(
                                    lineno,
                                    line,
                                    "Unindexed section name cannot contain wildcard character (*).",
                                )
                            step_names.append([n, int(i) if i else i, al])
                        if di:
                            step_names.append(["", int(di), al])
                    else:
                        parsing_errors.append(lineno, line, "Invalid section name")
                if "global" in [x[0] for x in step_names] and len(step_names) > 1:
                    parsing_errors.append(
                        lineno,
                        line,
                        "Global section cannot be shared with another step",
                    )
                if section_option is not None:
                    # this does not work directly because list parameter can have ,
                    # without having to really evaluate all complex expressions, we
                    # have to try to put syntax correctly pieces together.
                    try:
                        pieces = separate_options(section_option)
                        for option in pieces:
                            mo = SOS_SECTION_OPTION.match(option)
                            if mo:
                                opt_name, opt_value = mo.group("name", "value")
                                if opt_name in step_options:
                                    parsing_errors.append(
                                        lineno, line, "Duplicate options"
                                    )
                                step_options[opt_name] = opt_value
                            else:
                                parsing_errors.append(
                                    lineno, line, "Invalid section option"
                                )
                    except Exception as e:
                        parsing_errors.append(lineno, line, str(e))
                    if (
                        "EXECUTOR" in env.config["SOS_DEBUG"]
                        or "ALL" in env.config["SOS_DEBUG"]
                    ):
                        env.log_to_file(
                            "EXECUTOR",
                            "Header parsed with names {} and options {}".format(
                                step_names, step_options
                            ),
                        )
                for name in step_names:
                    prev_workflows = [x[0] for x in all_step_names if "*" not in x[0]]
                    for prev_name in all_step_names:
                        # auxiliary step
                        if (
                            name[1] is None
                            and prev_name[1] is None
                            and name[0] != prev_name[0]
                        ):
                            continue
                        # index not euqal (one of them can be None)
                        if name[1] != prev_name[1]:
                            continue
                        # index equal and one of them have wild card character
                        if "*" in name[0]:
                            names = [
                                x
                                for x in prev_workflows
                                if re.match(name[0].replace("*", ".*"), x)
                            ]
                        else:
                            names = [name[0]]
                        if "*" in prev_name:
                            prev_names = [
                                x
                                for x in prev_workflows
                                if re.match(prev_name[0].replace("*", ".*"), x)
                            ]
                        else:
                            prev_names = [prev_name[0]]
                        if len(set(prev_names) & set(names)) and "global" not in names:
                            parsing_errors.append(
                                lineno, line, f"Duplicate section name {names}"
                            )
                all_step_names.extend(step_names)
                if "global" in [x[0] for x in step_names]:
                    if step_options:
                        parsing_errors.append(
                            lineno, line, "Global section does not accept any option"
                        )
                    self.sections.append(SoS_Step(is_global=True))
                else:
                    # the second block is attached to
                    if comment_block == 2:
                        self.description = ""
                    self.sections.append(
                        SoS_Step(
                            self.content,
                            step_names,
                            step_options,
                            comment=self._last_comment,
                        )
                    )
                cursect = self.sections[-1]
                self.clear_comment()
                continue
            #
            # directive?
            mo = SOS_DIRECTIVE.match(line)
            if mo and not is_type_hint(line):
                # check previous expression before a new directive
                if cursect:
                    if not cursect.isValid():
                        parsing_errors.append(
                            cursect.lineno,
                            "".join(cursect.values[:5]),
                            f"Invalid {cursect.category()}: {cursect.error_msg}",
                        )
                    cursect.values = []
                    # allow multiple process-style actions
                    try:
                        cursect.wrap_script()
                    except Exception as e:
                        parsing_errors.append(
                            cursect.lineno, "".join(cursect.values[:5]), str(e)
                        )
                else:
                    self.sections.append(SoS_Step(is_global=True))
                    cursect = self.sections[-1]
                #
                directive_name = mo.group("directive_name")
                # newline should be kept in case of multi-line directive
                directive_value = mo.group("directive_value") + "\n"
                # is it an action??
                if directive_name in SOS_DIRECTIVES and directive_name != "parameter":
                    if cursect is None:
                        parsing_errors.append(
                            lineno,
                            line,
                            f"Directive {directive_name} is not allowed outside of a SoS step",
                        )
                        continue
                    cursect.add_directive(directive_name, directive_value, lineno)
                else:
                    if directive_name == "parameter":
                        if not directive_value.strip():
                            # ignore lines with only an empty parameter directive #1282
                            continue
                        if comment_block == 2:
                            self.description = ""
                        cursect.add_directive(
                            directive_name,
                            directive_value,
                            lineno,
                            comment=self._last_comment,
                        )
                    else:
                        # let us check if this is an acture action, or a type hint
                        cursect.add_script(directive_name, directive_value, lineno)
                self.clear_comment()
                continue
            # if section is in script mode?
            if cursect and cursect.isValid() and cursect.category() == "script":
                # if the script is indented and the line is not, the script
                # is ended.
                if not line[0].isspace() and cursect.indented_script():
                    try:
                        cursect.wrap_script()
                    except Exception as e:
                        parsing_errors.append(lineno, line, str(e))
                else:
                    cursect.extend(line)
                    continue

            # all others?
            if not cursect:
                self.sections.append(SoS_Step(is_global=True))
                cursect = self.sections[-1]
                cursect.add_statement(line, lineno)
                self.clear_comment()
                continue
            #
            if cursect.empty() or cursect.category() != "statements":
                # new statement
                cursect.add_statement(line, lineno)
            else:
                # existing one
                cursect.extend(line)
        #
        # check the last expression before a new directive
        if cursect:
            if not cursect.isValid():
                parsing_errors.append(
                    cursect.lineno,
                    "".join(cursect.values[:5]),
                    f"Invalid {cursect.category()}: {cursect.error_msg}",
                )
            else:
                try:
                    cursect.finalize()
                except Exception as e:
                    parsing_errors.append(
                        cursect.lineno, "".join(cursect.values[:5]), str(e)
                    )

        #
        # if there is any parsing error, raise an exception
        #
        # if there is no section in the script, we create a default section with global
        # definition being the content.
        global_parameters: Dict = {}
        if not [x for x in self.sections if not x.is_global]:
            self.sections.append(SoS_Step(self.content, [("default", None, None)]))
            for section in [x for x in self.sections if x.is_global]:
                if self.sections[-1].task != "":
                    parsing_errors.append(
                        cursect.lineno,
                        "Invalid section",
                        "Cannot define multiple default sections with a task in between.",
                    )
                self.sections[-1].statements.extend(section.statements)
                self.sections[-1].task = section.task
                self.sections[-1].task_params = section.task_params
                self.global_stmts = ""
                global_parameters.update(section.parameters)
            # The sections should have been finalized so there is no need to finalize
            # again. In particular, finalizing a section would reset existing task #833
            # self.sections[-1].finalize()
        else:
            # as the last step, let us insert the global section to all sections
            for sec in [x for x in self.sections if x.is_global]:
                for statement in sec.statements:
                    if statement[0] == ":":
                        parsing_errors.append(
                            cursect.lineno,
                            f"{statement[1]}:{statement[2]}",
                            "Global section cannot contain sos input, ouput, and task statements",
                        )
                    else:
                        self.global_stmts += statement[1]
                global_parameters.update(sec.parameters)
        # remove the global section after inserting it to each step of the process
        self.sections = [x for x in self.sections if not x.is_global]
        #
        if parsing_errors.errors:
            raise parsing_errors
        #
        for section in self.sections:
            section.global_stmts = self.global_stmts
            section.global_parameters = global_parameters
            section.parameters.update(global_parameters)
            #
            section.md5 = textMD5(section.get_tokens())

    def workflow(
        self, workflow_name: Optional[str] = None, use_default: bool = True
    ) -> SoS_Workflow:
        """Return a workflow with name_step+name_step specified in wf_name
        This function might be called recursively because of nested
        workflow."""
        if workflow_name is None and not use_default:
            return SoS_Workflow(self.content, "", "", self.sections, self.global_stmts)
        allowed_steps = None
        if not workflow_name:
            wf_name = ""
        else:
            # if consists of multiple workflows
            if "+" in workflow_name:
                wfs = []
                for wf in workflow_name.split("+"):
                    if not SOS_SUBWORKFLOW.match(wf):
                        raise ValueError(f"Incorrect workflow name {workflow_name}")
                    # if this is a combined workflow, extra_section might be specied.
                    wfs.append(self.workflow(wf))
                combined_wf = wfs[0]
                for wf in wfs[1:]:
                    combined_wf.extend(wf)
                combined_wf.name = workflow_name
                return combined_wf
            # if a single workflow
            # workflow_10:15 etc
            mo = SOS_SUBWORKFLOW.match(workflow_name)
            if not mo:
                raise ValueError(f"Incorrect workflow name {workflow_name}")
            wf_name, allowed_steps = mo.group("name", "steps")
        # check source
        if not wf_name:
            if len(self.workflows) == 1:
                wf_name = list(self.workflows)[0]
            elif self.default_workflow:
                wf_name = self.default_workflow
            elif "default" in self.workflows or "" in self.workflows:
                wf_name = "default"
            else:
                raise ValueError(
                    "Name of workflow should be specified because "
                    "the script defines more than one pipelines without a default one. "
                    "Available pipelines are: {}.".format(", ".join(self.workflows))
                )
        elif wf_name not in self.workflows and wf_name != "default":
            raise ValueError(
                f'Workflow {wf_name} is undefined. Available workflows are: {", ".join(self.workflows)}'
            )

        return SoS_Workflow(
            self.content, wf_name, allowed_steps, self.sections, self.global_stmts
        )

    def print_help(self, script_name: str):
        """print a help message from the script"""
        textWidth = max(60, shutil.get_terminal_size((80, 20)).columns)

        if len(script_name) > 20:
            print(f"usage: sos run {script_name}")
            print(
                "               [workflow_name | -t targets] [options] [workflow_options]"
            )
        else:
            print(
                f"usage: sos run {script_name} [workflow_name | -t targets] [options] [workflow_options]"
            )
        print(
            "  workflow_name:        Single or combined workflows defined in this script"
        )
        print("  targets:              One or more targets to generate")
        print(
            '  options:              Single-hyphen sos parameters (see "sos run -h" for details)'
        )
        print("  workflow_options:     Double-hyphen workflow-specific parameters")
        description = [x.lstrip("# ").strip() for x in self.description]
        description = textwrap.dedent("\n".join(description)).strip()
        if description:
            print("\n" + description)
        #
        print("\nWorkflows:")
        print("  " + "\n  ".join(self.workflows))
        #
        global_parameters = {}
        for section in self.sections:
            global_parameters.update(section.global_parameters)
        if global_parameters:
            print("\nGlobal Workflow Options:")
            for name, (value, comment) in global_parameters.items():
                par_str = f"  {format_par(name, value)}"
                print(par_str)
                if comment:
                    print(
                        "\n".join(
                            textwrap.wrap(
                                comment,
                                width=textWidth,
                                initial_indent=" " * 24,
                                subsequent_indent=" " * 24,
                            )
                        )
                    )
        #
        print("\nSections")
        for section in self.sections:
            section.show()
