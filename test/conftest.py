import pytest
import uuid
import os
import shutil
import tempfile
import textwrap
import pathlib
import yaml


@pytest.fixture
def config_factory():
    filename = tempfile.NamedTemporaryFile(suffix='.yml', delete=False).name

    def get_config(text_or_dict):
        with open(filename, 'w') as conf:
            if isinstance(text_or_dict, str):
                conf.write(textwrap.dedent(text_or_dict))
            elif isinstance(text_or_dict, dict):
                yaml.dump(text_or_dict, conf)
            else:
                raise ValueError(
                    'A text or dictionary is expected for config_factory.')
        return filename

    yield get_config
    os.remove(filename)


@pytest.fixture
def script_factory():
    filename = tempfile.NamedTemporaryFile(suffix='.sos', delete=False).name

    def get_script(text):
        with open(filename, 'w') as conf:
            conf.write(textwrap.dedent(text))
        return filename

    yield get_script
    os.remove(filename)


@pytest.fixture
def temp_factory():

    temp_fds = []
    temp_dirs = []

    def get_tempfiles(*args, **kwargs):
        content = kwargs.get('content', None)
        for names in args:
            if isinstance(names, str):
                names = [names]
            for name in names:
                if content is None:
                    pathlib.Path(name).touch()
                else:
                    with open(name, 'w') as tf:
                        tf.write(content)
                temp_fds.append(name)
        if 'dir' in kwargs:
            if isinstance(kwargs['dir'], str):
                dirs = [kwargs['dir']]
            else:
                dirs = kwargs['dir']
            for dir in dirs:
                if os.path.isdir(dir):
                    shutil.rmtree(dir)
                os.makedirs(dir, exist_ok=True)
                temp_dirs.append(dir)
        return temp_fds, temp_dirs

    yield get_tempfiles

    for temp_file in temp_fds:
        try:
            if os.path.isfile(temp_file):
                os.remove(temp_file)
        except Exception:
            pass

    for temp_dir in temp_dirs:
        try:
            if os.path.isdir(temp_dir):
                shutil.rmtree(temp_dir)
        finally:
            pass


@pytest.fixture
def clear_now_and_after():
    # This fixture takes one or more file names or directory names and will
    # remove them if they exist, and also after the tests are completed.
    temp_fds = []

    def clear_files_and_dirs():
        for temp_fd in temp_fds:
            try:
                if os.path.isfile(temp_fd):
                    os.remove(temp_fd)
                elif os.path.isdir(temp_fd):
                    shutil.rmtree(temp_fd)
            except Exception:
                pass

    def get_names(*args):
        for names in args:
            if isinstance(names, str):
                temp_fds.append(names)
            else:
                temp_fds.extend(names)

        clear_files_and_dirs()
        return temp_fds

    yield get_names

    clear_files_and_dirs()
