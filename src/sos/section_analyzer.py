#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import ast

from collections.abc import Mapping, Sequence
from typing import Any, Dict, Optional

from .eval import SoS_eval, accessed_vars, used_in_func, SoS_exec
from .parser import SoS_Step
from .targets import dynamic, remote, sos_targets, sos_step, named_output, file_target
from .utils import env
from .executor_utils import __null_func__, prepare_env, strip_param_defs
from .syntax import SOS_TARGETS_OPTIONS


def get_param_of_function(name, param_list, extra_dict={}):
    tree = ast.parse(f'__null_func__({param_list})')
    # x.func can be an attribute (e.g. a.b()) and do not have id
    funcs = [
        x for x in ast.walk(tree) if x.__class__.__name__ == 'Call' and
        hasattr(x.func, 'id') and x.func.id == name
    ]
    params = []
    for func in funcs:
        for arg in func.args:
            try:
                params.append([ast.literal_eval(arg)])
            except Exception as e:
                if 'STEP' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                        'SOS_DEBUG']:
                    env.log_to_file(
                        'STEP',
                        f'Failed to evaluate parameter of function {name} from {param_list}: {e}'
                    )
                try:
                    params.append([
                        eval(
                            compile(
                                ast.Expression(body=arg),
                                filename='<string>',
                                mode="eval"), extra_dict)
                    ])
                except Exception as e:
                    if 'STEP' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                            'SOS_DEBUG']:
                        env.log_to_file(
                            'STEP',
                            f'Failed to evaluate parameter of function {name} from {param_list}: {e}'
                        )
        for kwarg in func.keywords:
            try:
                params.append([kwarg.arg, ast.literal_eval(kwarg.value)])
            except Exception as e:
                if 'STEP' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                        'SOS_DEBUG']:
                    env.log_to_file(
                        'STEP',
                        f'Failed to evaluate parameter of function {name} from {param_list}: {e}'
                    )
                try:
                    params.append([
                        kwarg.arg,
                        eval(
                            compile(
                                ast.Expression(body=kwarg.value),
                                filename='<string>',
                                mode="eval"), extra_dict)
                    ])
                except Exception as e:
                    if 'STEP' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                            'SOS_DEBUG']:
                        env.log_to_file(
                            'STEP',
                            f'Failed to evaluate parameter of function {name} from {param_list}: {e}'
                        )
    return params


def get_names_of_param(name, param_list, extra_dict={}):
    tree = ast.parse(f'__null_func__({param_list})')
    kwargs = [
        x for x in ast.walk(tree)
        if x.__class__.__name__ == 'keyword' and x.arg == name
    ]

    values = []
    for kwarg in kwargs:
        # Python 3.8 class name is named Constant, not Str
        values.extend([
            x.s
            for x in ast.walk(kwarg.value)
            if x.__class__.__name__ in ('Str',
                                        'Constant') and isinstance(x.s, str)
        ])
    return values


def get_num_of_args_and_names_of_kwargs(param_list):
    tree = ast.parse(f'__null_func__({param_list})')
    # func is the __null_func__
    func = tree.body[0].value
    nargs = len(func.args)
    keywords = [x.arg for x in func.keywords]
    return nargs, keywords


def find_statement(section, name):
    stmt_idx = [
        idx for idx, x in enumerate(section.statements)
        if x[0] == ':' and x[1] == name
    ]
    if not stmt_idx:
        return None
    elif len(stmt_idx) == 1:
        return stmt_idx[0]
    else:
        raise RuntimeError(
            f'More than one step {name} statement are specified in step {section.step_name()}'
        )


def no_output_from(*args, **kwargs):
    raise SyntaxError(
        'Function output_from can only be used in input or depends statements')


def no_named_output(*args, **kwargs):
    raise SyntaxError(
        'Function named_output can only be used in input or depends statements')


def no_sos_step(*args, **kwargs):
    raise SyntaxError('Target sos_step can only be used in depends statements')


def no_sos_variable(*args, **kwargs):
    raise SyntaxError(
        'Target sos_variable can only be used in depends statements')


def get_changed_vars(section: SoS_Step):
    '''changed vars are variables that are "shared" and therefore "provides"
    to others '''
    if 'shared' not in section.options:
        return set()

    changed_vars = set()
    svars = section.options['shared']
    if isinstance(svars, str):
        changed_vars.add(svars)
        svars = {svars: svars}
    elif isinstance(svars, Sequence):
        for item in svars:
            if isinstance(item, str):
                changed_vars.add(item)
            elif isinstance(item, Mapping):
                changed_vars |= set(item.keys())
            else:
                raise ValueError(
                    f'Option shared should be a string, a mapping of expression, or list of string or mappings. {svars} provided'
                )
    elif isinstance(svars, Mapping):
        changed_vars |= set(svars.keys())
    else:
        raise ValueError(
            f'Option shared should be a string, a mapping of expression, or list of string or mappings. {svars} provided'
        )
    return changed_vars


def get_environ_vars(section):
    # environ variables are variables should be inserted from outside
    # which is basically the sos_variable(var)
    environ_vars = set()
    depends_idx = find_statement(section, 'depends')
    if depends_idx is not None:
        value = section.statements[depends_idx][2]
        args = get_param_of_function(
            'sos_variable', value, extra_dict=env.sos_dict.dict())
        for arg in args:
            if len(arg) == 2:
                raise SyntaxError(
                    'sos_variable does not accept keyword argument')
            environ_vars.add(arg[0])
    if section.task:
        # 1281
        environ_vars |= accessed_vars(section.task_params, mode='eval')
    return environ_vars


def get_all_used_vars(section):
    '''Get variables which are variables used by input statement and statements before it'''
    all_used_vars = set()
    for statement in section.statements:
        if statement[0] == '=':
            all_used_vars |= accessed_vars('='.join(statement[1:3]))
        elif statement[0] == '!':
            all_used_vars |= accessed_vars(statement[1])
        elif statement[0] == ':':
            all_used_vars |= accessed_vars(statement[2], mode='eval')
            if statement[1] != 'input':
                continue
            if 'paired_with' in statement[2]:
                try:
                    pws = get_names_of_param(
                        'paired_with',
                        statement[2],
                        extra_dict=env.sos_dict.dict())
                    all_used_vars |= set(pws)
                except Exception as e:
                    raise ValueError(
                        f'Failed to parse parameter paired_with: {e}')
            if 'group_with' in statement[2]:
                try:
                    pws = get_names_of_param(
                        'group_with',
                        statement[2],
                        extra_dict=env.sos_dict.dict())
                    all_used_vars |= set(pws)
                except Exception as e:
                    raise ValueError(
                        f'Failed to parse parameter group_with: {e}')
            if 'for_each' in statement[2]:
                try:
                    pws = get_names_of_param(
                        'for_each',
                        statement[2],
                        extra_dict=env.sos_dict.dict())
                    for pw in pws:
                        all_used_vars |= set(pw.split(','))
                except Exception as e:
                    raise ValueError(f'Failed to parse parameter for_each: {e}')
    if section.task:
        all_used_vars |= accessed_vars(section.task)

    # now we have a list of global variables that are actually used in the functions
    # this is specifically designed to handle the last case in #1225
    func_with_vars = [
        y for x, y in used_in_func(section.global_stmts).items()
        if x in all_used_vars
    ]
    return set.union(all_used_vars, *func_with_vars)


def get_signature_vars(section):
    '''Get signature variables which are variables that will be
    saved with step signatures'''

    # signature vars should contain parameters defined in global section
    # #1155, we however need to keep local parameters
    signature_vars = set(section.parameters.keys()) - set(
        section.global_parameters.keys())
    signature_vars |= set(
        section.global_parameters.keys()
        & accessed_vars(strip_param_defs(section.global_stmts)))

    input_idx = find_statement(section, 'input')
    after_input_idx = 0 if input_idx is None else input_idx + 1

    for statement in section.statements[after_input_idx:]:
        if statement[0] == '=':
            signature_vars |= accessed_vars('='.join(statement[1:3]))
        elif statement[0] == '!':
            signature_vars |= accessed_vars(statement[1])
    # finally, tasks..
    if section.task:
        signature_vars |= accessed_vars(section.task)

    return {x for x in signature_vars if not x.startswith('__')}


def get_step_depends(section):

    step_depends: sos_targets = sos_targets()
    dynamic_depends = True

    input_idx = find_statement(section, 'input')
    depends_idx = find_statement(section, 'depends')
    for stmt_idx in ([] if input_idx is None else [input_idx]) + (
        [] if depends_idx is None else [depends_idx]):
        # input statement
        stmt = section.statements[stmt_idx][2]
        if 'sos_step' in stmt:
            step_depends.extend([sos_step(x) for x in get_sos_step_steps(stmt)])
        if 'output_from' in stmt:
            step_depends.extend([
                sos_step(x)
                for x in get_output_from_steps(stmt, section.last_step)
            ])
        if 'named_output' in stmt:
            # there can be multiple named_output calls
            pars = get_param_of_function(
                'named_output', stmt, extra_dict=env.sos_dict.dict())
            for par in pars:
                # a single argument
                if len(par) == 1:
                    if not isinstance(par[0], str):
                        raise ValueError(
                            f'Value for named_output can only be a name (str): {par[0]} provided'
                        )
                    step_depends.extend(named_output(par[0]))
                else:
                    if par[0] in SOS_TARGETS_OPTIONS:
                        continue
                    elif par[0] == 'name':
                        if not isinstance(par[1], str):
                            raise ValueError(
                                f'Value for named_output can only be a name (str): {par[1]} provided'
                            )
                        step_depends.extend(named_output(par[1]))
                    else:
                        raise ValueError(
                            f'Unacceptable keyword argument {par[0]} for named_output()'
                        )

    if depends_idx is not None:
        value = section.statements[depends_idx][2]
        try:
            svars = ['output_from', 'named_output']
            old_values = {
                x: env.sos_dict.dict()[x]
                for x in svars
                if x in env.sos_dict.dict()
            }
            # output_from and named_output has been processed
            env.sos_dict.quick_update({
                'output_from': lambda *args, **kwargs: None,
                'named_output': lambda *args, **kwargs: None,
                'traced': lambda *args, **kwargs: sos_targets(*args, **kwargs)
            })
            args, kwargs = SoS_eval(
                f'__null_func__({value})', extra_dict=env.sos_dict.dict())
            if any(isinstance(x, (dynamic, remote)) for x in args):
                dynamic_depends = True
            else:
                step_depends.extend(sos_targets(*args, **kwargs))
                dynamic_depends = False
        except SyntaxError:
            raise
        except Exception as e:
            if 'STEP' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                    'SOS_DEBUG']:
                env.log_to_file(
                    'STEP',
                    f"Args {value} in depends cannot be determined: {e}")
        finally:
            [env.sos_dict.dict().pop(x) for x in svars]
            env.sos_dict.quick_update(old_values)
    return step_depends, dynamic_depends


def get_step_input(section, default_input):
    '''Find step input
    '''
    step_input: sos_targets = sos_targets()
    dynamic_input = True

    # look for input statement.
    input_idx = find_statement(section, 'input')
    # #1270. If there is any statement before input:, it might create input or remove input,
    # which essentially make input undetermined before actually run it.
    if input_idx is None or (input_idx != 0 and any(
            x[0] == '!' for x in section.statements[:input_idx])):
        return step_input, dynamic_input

    # input statement
    stmt = section.statements[input_idx][2]
    try:
        svars = ['output_from', 'named_output', 'sos_step', 'sos_variable']
        old_values = {
            x: env.sos_dict.dict()[x]
            for x in svars
            if x in env.sos_dict.dict()
        }
        env.sos_dict.quick_update({
            'output_from': lambda *args, **kwargs: None,
            'named_output': lambda *args, **kwargs: None,
            'traced': lambda *args, **kwargs: sos_targets(*args, **kwargs),
            'sos_step': no_sos_step,
            'sos_variable': no_sos_variable,
        })
        args, kwargs = SoS_eval(
            f'__null_func__({stmt})', extra_dict=env.sos_dict.dict())
        if not args:
            if default_input is None:
                step_input = sos_targets()
            else:
                step_input = default_input
        elif not any(isinstance(x, (dynamic, remote)) for x in args):
            step_input = sos_targets(*args)
    except SyntaxError:
        raise
    except Exception as e:
        # if anything is not evalutable, keep Undetermined
        env.logger.debug(
            f'Input of step {section.name if section.index is None else f"{section.name}_{section.index}"} is set to Undertermined: {e}'
        )
        # expression ...
        step_input = sos_targets(_undetermined=stmt)
    finally:
        [env.sos_dict.dict().pop(x) for x in svars]
        env.sos_dict.quick_update(old_values)
    return step_input, dynamic_input


def get_step_output(section, default_output, analysis_type):
    '''determine step output'''
    #
    # There are three analysis_style:
    #
    #  'default': used for initial pass, we do not have any
    #      default_output, and we just try to get some information
    #      if we can.
    #
    #  'forward': used when adding forward step. We do not care
    #       about default_output.
    #
    #  'backward': used when adding as backwwad step. Smetimes we have
    #       to figure out output.
    #
    step_output: sos_targets = sos_targets()

    if 'provides' in section.options and default_output:
        step_output = default_output

    # look for output statement.
    output_idx = find_statement(section, 'output')
    if output_idx is None:
        return step_output

    # if the step is referred to by named_output, sos_step etc, we do not
    # care about their output. In terms of bug #1379, perhaps the step would
    # be added to the DAG multiple times, it will be run only once.
    if analysis_type == 'backward' and (default_output is None or all(
            isinstance(x, sos_step) for x in default_output)):
        return step_output

    # # if the output is of type named_output, we do not need to care about
    # # the exact output either. We just need to check if there are other
    # # named output
    n_args, name_kwargs = get_num_of_args_and_names_of_kwargs(
        section.statements[output_idx][2])
    name_kwargs = [x for x in name_kwargs if x not in SOS_TARGETS_OPTIONS]
    if name_kwargs:
        step_output.extend([named_output(x) for x in name_kwargs])

    # now, if we are referred by a filename, we have to figure out what
    # these filenames are... if there is only one argument, let us
    # be lazy and assume that the output is the one we want.. Note that
    # we are assuming that the step does not have any substep.
    # if n_args + len(name_kwargs) == 1:
    #     return step_output

    # if we do have output, we have to evaluate starting from input...
    for statement in section.statements[:output_idx + 1]:
        if statement[0] == ':' and statement[1] == 'depends':
            continue
        if statement[0] == '!':
            if analysis_type == 'backward':
                try:
                    SoS_exec(statement[1], return_result=False)
                except Exception as e:
                    raise f'Failed to evaluate an statement "{value}" of an auxiliary step: {e}'
            continue

        if statement[1] == 'input' and analysis_type != 'backward':
            continue

        value = statement[2]
        try:
            svars = ['output_from', 'named_output', 'sos_step', 'sos_variable']
            old_values = {
                x: env.sos_dict.dict()[x]
                for x in svars
                if x in env.sos_dict.dict()
            }
            env.sos_dict.quick_update({
                'output_from': no_output_from,
                'named_output': no_named_output,
                'sos_step': no_sos_step,
                'sos_variable': no_sos_variable,
            })
            args, kwargs = SoS_eval(
                f'__null_func__({value})', extra_dict=env.sos_dict.dict())
            if any(isinstance(x, (dynamic, remote)) for x in args):
                raise ValueError(
                    f'Auxiliary step does not allow dynamic or remote input or output: {value} provided'
                )
            if statement[1] == 'input':
                step_input = sos_targets(
                    *args, **{
                        x: y
                        for x, y in kwargs.items()
                        if x not in SOS_TARGETS_OPTIONS
                    })
                env.sos_dict.set('_input', step_input)
                env.sos_dict.set('step_input', step_input)
            else:
                step_output = sos_targets(
                    *args, **{
                        x: y
                        for x, y in kwargs.items()
                        if x not in SOS_TARGETS_OPTIONS
                    })
            if kwargs:
                step_output.extend(named_output(x) for x in kwargs.keys())
        except SyntaxError:
            raise
        except Exception as e:
            if 'STEP' in env.config['SOS_DEBUG'] or 'ALL' in env.config[
                    'SOS_DEBUG']:
                env.log_to_file('STEP',
                                f"Args {value} cannot be determined: {e}")
            # usually we want to get the exact output. However in the case when
            # the step is referred by named_output(), it is ok for us to not
            # know the details.
            if analysis_type == 'backward' and (n_args > 0 or not all(
                    isinstance(x, named_output) for x in default_output)):
                raise RuntimeError(
                    f'Failed to determine input "{value}" of an auxiliary step: {e}'
                )
        finally:
            [env.sos_dict.dict().pop(x) for x in svars]
            env.sos_dict.quick_update(old_values)

    if 'provides' in section.options and default_output is not None:
        for out in default_output:
            # 981
            if not isinstance(out, sos_step) and out not in step_output:
                raise ValueError(
                    f'Defined output fail to produce expected output: {step_output} generated, {default_output} expected.'
                )
    return step_output


def get_sos_step_steps(stmt):
    '''
    Extract sos_step(x) from statement
    '''
    opt_values = get_param_of_function(
        'sos_step', stmt, extra_dict=env.sos_dict.dict())
    for value in opt_values:
        if len(value) != 1:
            raise ValueError('sos_step only accept one and only one parameter')
    return [x[0] for x in opt_values]


def get_output_from_steps(stmt, last_step):
    '''
    Extract output_from(1), output_from('step_1'), and output_from([1, 2])
    to determine dependent steps
    '''
    opt_values = get_param_of_function(
        'output_from', stmt, extra_dict=env.sos_dict.dict())

    def step_name(val):
        if isinstance(val, str):
            return val
        elif isinstance(val, int):
            if val == -1:
                if last_step is None:
                    # there is a case where a regular step is checked as auxiliary step.
                    # we will postpone the decision later because the step might not be
                    # used as such
                    return None
                return last_step
            if '_' in env.sos_dict['step_name']:
                return f"{env.sos_dict['step_name'].rsplit('_',1)[0]}_{val}"
            else:
                return str(val)
        else:
            raise ValueError(f'Invalid value {val} for output_from() function')

    res = []
    for value in opt_values:
        if len(value) == 1:
            # regular argument
            value = value[0]
        elif value[0] == 'steps':
            value = value[1]
        elif value[0] in SOS_TARGETS_OPTIONS:
            continue
        else:
            raise ValueError(
                f'Unacceptable keyword argument {value[0]} for function output_from'
            )
        if isinstance(value, (int, str)):
            res.append(step_name(value))
        elif isinstance(value, Sequence):
            res.extend([step_name(x) for x in value])
        else:
            raise ValueError(f'Invalid value for input option from {value}')
    return [x for x in res if x is not None]


# analysis_cache = {}


def analyze_section(section: SoS_Step,
                    default_input: Optional[sos_targets] = None,
                    default_output: Optional[sos_targets] = None,
                    context=None,
                    analysis_type='initial') -> Dict[str, Any]:
    '''Analyze a section for how it uses input and output, what variables
    it uses, and input, output, etc.'''

    # analysis_key = (section.md5, section.step_name(),
    #     default_input.target_name() if hasattr(default_input, 'target_name') else '',
    #     default_output.target_name() if hasattr(default_output, 'target_name') else '', vars_and_output_only)
    #if analysis_key in analysis_cache:
    #    return analysis_cache[analysis_key]

    # use a fresh env for analysis
    new_env, old_env = env.request_new()
    try:
        prepare_env(section.global_def, section.global_vars,
                    {} if context is None else context)

        env.sos_dict.set('step_name', section.step_name())
        env.sos_dict.set('__null_func__', __null_func__)
        if 'STEP' in env.config['SOS_DEBUG'] or 'ALL' in env.config['SOS_DEBUG']:
            env.log_to_file(
                'STEP',
                f'Analyzing {section.step_name()} {"(output only)" if analysis_type == "initial" else ""}'
            )

        res = {
            'step_name':
                section.step_name(),
            'step_output':
                get_step_output(section, default_output, analysis_type),
            # variables starting with __ are internals...
            'environ_vars':
                get_environ_vars(section),
            'signature_vars':
                get_signature_vars(section),
            'changed_vars':
                get_changed_vars(section)
        }
        if analysis_type != 'initial':
            inps = get_step_input(section, default_input)
            res['step_input'] = inps[0]
            res['dynamic_input'] = inps[1]
            deps = get_step_depends(section)
            res['step_depends'] = deps[0]
            res['dynamic_depends'] = deps[1]
    # analysis_cache[analysis_key] = res
    finally:
        # restore env
        env.restore_to_old(new_env, old_env)

    # #1225
    # The global section can contain a lot of variables, some of which can be large. Here we
    # found all variables that will be used in the step, including ones used in substep (signature_vars)
    # and ones that will be used in input statement etc.
    section.global_vars = {
        x: y
        for x, y in section.global_vars.items()
        if x in get_all_used_vars(section)
    }
    return res
