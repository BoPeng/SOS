#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.


import os
import subprocess
import sys
import traceback
import contextlib
import psutil
import zmq

from io import StringIO

from .eval import SoS_exec, stmtHash
from .targets import (RemovedTarget, RuntimeInfo, UnavailableLock,
                      UnknownTarget)
from .executor_utils import (prepare_env, clear_output, verify_input,
        reevaluate_output, validate_step_sig)

from .utils import (StopInputGroup, TerminateExecution, ArgumentError, env)


@contextlib.contextmanager
def stdoutIO():
    oldout = sys.stdout
    olderr = sys.stderr
    stdout = StringIO()
    stderr = StringIO()
    sys.stdout = stdout
    sys.stderr = stderr
    yield stdout, stderr
    sys.stdout = oldout
    sys.stderr = olderr


def execute_substep(stmt, proc_vars={}, step_md5=None, step_tokens=[],
    shared_vars=[], config={}, capture_output=False):
    '''Execute a substep with specific input etc

    Substep executed by this function should be self-contained.
    That is to say, substep should not contain tasks or nested
    workflows. Substeps containing those elements should be executed
    with the step (not concurrently).

    The executor checks step signatures and might skip the substep if it has
    been executed and the signature matches.

    The executor accepts connections to the controller, and a socket using
    which the results will be returned. However, the calling process should
    take care of the connection and disconnection of controller sockets and
    this function only takes care of the connection and disconnection of
    result socket.

    The return value should be a dictionary with the following keys:

    index: index of the substep within the step
    ret_code: (all) return code, 0 for successful
    sig_skipped: (optional) return if the step is skipped due to signature
    shared: (optional) shared variable as specified by 'shared_vars'
    stdout: (optional) if capture_output is True (in interactive mode)
    stderr: (optional) if capture_output is True (in interactive mode)
    exception: (optional) if an exception occures
    '''
    assert not env.zmq_context.closed
    assert not env.controller_push_socket.closed
    assert not env.controller_req_socket.closed
    assert not env.signature_push_socket.closed
    assert not env.signature_req_socket.closed
    assert 'step_id' in proc_vars
    assert '_index' in proc_vars
    assert 'result_push_socket' in config["sockets"]

    try:
        res_socket = env.zmq_context.socket(zmq.PUSH)
        res_socket.connect(f'tcp://127.0.0.1:{config["sockets"]["result_push_socket"]}')
        res = _execute_substep(stmt=stmt, proc_vars=proc_vars, step_md5=step_md5, step_tokens=step_tokens,
            shared_vars=shared_vars, config=config, capture_output=capture_output)
        res_socket.send_pyobj(res)
    finally:
        res_socket.close()

def _execute_substep(stmt, proc_vars, step_md5, step_tokens,
    shared_vars, config, capture_output):
    # passing configuration and port numbers to the subprocess
    env.config.update(config)
    # prepare a working environment with sos symbols and functions
    prepare_env()

    # update it with variables passed from master process
    env.sos_dict.quick_update(proc_vars)
    sig = None if env.config['sig_mode'] == 'ignore' or env.sos_dict['_output'].unspecified() else RuntimeInfo(
        step_md5, step_tokens,
        env.sos_dict['_input'],
        env.sos_dict['_output'],
        env.sos_dict['_depends'],
        env.sos_dict['__signature_vars__'],
        shared_vars=shared_vars)
    outmsg = ''
    errmsg = ''
    try:
        if sig:
            matched = validate_step_sig(sig)
            if matched:
                # avoid sig being released in the final statement
                sig = None
                # complete case: concurrent ignore without task
                env.controller_push_socket.send_pyobj(['progress', 'substep_ignored', env.sos_dict['step_id']])
                return {'index': env.sos_dict['_index'], 'ret_code': 0, 'sig_skipped': 1, 'output': matched['output'],
                    'shared': matched['vars']}
            sig.lock()

        # check if input and depends targets actually exist
        #
        # if depends on a sos_variable but the variable is not actually used in
        # the substep, it is ok to ignore it. If the variable is used in the substep
        # it should have been included as part of the signature variables.
        verify_input(ignore_sos_variable=True)

        if capture_output:
            with stdoutIO() as (out, err):
                SoS_exec(stmt, return_result=False)
                outmsg = out.getvalue()
                errmsg = err.getvalue()
        else:
            SoS_exec(stmt, return_result=False)
        if env.sos_dict['step_output'].undetermined():
            # the pool worker does not have __null_func__ defined
            env.sos_dict.set('_output', reevaluate_output())
        res = {'index': env.sos_dict['_index'], 'ret_code': 0}
        if sig:
            sig.set_output(env.sos_dict['_output'])
            # sig.write will use env.signature_push_socket
            if sig.write():
                res.update({'output': sig.content['output'], 'shared': sig.content['end_context']})
        if capture_output:
            res.update({'stdout': outmsg, 'stderr': errmsg})
        # complete case: concurrent execution without task
        env.controller_push_socket.send_pyobj(['progress', 'substep_completed', env.sos_dict['step_id']])
        return res
    except (StopInputGroup, TerminateExecution, UnknownTarget, RemovedTarget, UnavailableLock) as e:
        clear_output()
        res = {'index': env.sos_dict['_index'], 'ret_code': 1, 'exception': e}
        if capture_output:
            res.update({'stdout': outmsg, 'stderr': errmsg})
        return res
    except (KeyboardInterrupt, SystemExit) as e:
        clear_output()
        # Note that KeyboardInterrupt is not an instance of Exception so this piece is needed for
        # the subprocesses to handle keyboard interrupt. We do not pass the exception
        # back to the master process because the master process would handle KeyboardInterrupt
        # as well and has no chance to handle the returned code.
        procs = psutil.Process().children(recursive=True)
        if procs:
            if env.verbosity > 2:
                env.logger.info(
                    f'{os.getpid()} interrupted. Killing subprocesses {" ".join(str(x.pid) for x in procs)}')
            for p in procs:
                p.terminate()
            gone, alive = psutil.wait_procs(procs, timeout=3)
            if alive:
                for p in alive:
                    p.kill()
            gone, alive = psutil.wait_procs(procs, timeout=3)
            if alive:
                for p in alive:
                    env.logger.warning(f'Failed to kill subprocess {p.pid}')
        elif env.verbosity > 2:
            env.logger.info(f'{os.getpid()} interrupted. No subprocess.')
        raise e
    except subprocess.CalledProcessError as e:
        clear_output()
        # cannot pass CalledProcessError back because it is not pickleable
        res = {'index': env.sos_dict['_index'], 'ret_code': e.returncode, 'exception': RuntimeError(e.stderr)}
        if capture_output:
            res.update({'stdout': outmsg, 'stderr': errmsg})
        return res
    except ArgumentError as e:
        clear_output()
        return {'index': env.sos_dict['_index'], 'ret_code': 1, 'exception': e}
    except Exception as e:
        clear_output()
        error_class = e.__class__.__name__
        cl, exc, tb = sys.exc_info()
        msg = ''
        for st in reversed(traceback.extract_tb(tb)):
            if st.filename.startswith('script_'):
                code = stmtHash.script(st.filename)
                line_number = st.lineno
                code = '\n'.join([f'{"---->" if i+1 == line_number else "     "} {x.rstrip()}' for i,
                                  x in enumerate(code.splitlines())][max(line_number - 3, 0):line_number + 3])
                msg += f'''\
{st.filename} in {st.name}
{code}
'''
        detail = e.args[0] if e.args else ''
        res = {'index': env.sos_dict['_index'], 'ret_code': 1, 'exception': RuntimeError(f'''
---------------------------------------------------------------------------
{error_class:42}Traceback (most recent call last)
{msg}
{error_class}: {detail}''') if msg else RuntimeError(f'{error_class}: {detail}')}
        if capture_output:
            res.update({'stdout': outmsg, 'stderr': errmsg})
        return res
    finally:
        # release the lock even if the process becomes zombie? #871
        if sig:
            sig.release(quiet=True)