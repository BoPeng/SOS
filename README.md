[![Anaconda-Server Badge](https://anaconda.org/conda-forge/sos/badges/version.svg)](https://anaconda.org/conda-forge/sos)
[![PyPI version](https://badge.fury.io/py/sos.svg)](https://badge.fury.io/py/sos)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.1291524.svg)](https://doi.org/10.5281/zenodo.1291524)
[![Join the chat at https://gitter.im/vatlab/SoS](https://badges.gitter.im/vatlab/SoS.svg)](https://gitter.im/vatlab/SoS?utm_source=badge&utm_medium=badge&utm_campaign=pr-badge&utm_content=badge)
[![Build Status](https://travis-ci.org/vatlab/SoS.svg?branch=master)](https://travis-ci.org/vatlab/SoS)
[![Build Status](https://ci.appveyor.com/api/projects/status/x092eusa0tta3msw?svg=true
)](https://ci.appveyor.com/project/BoPeng/sos)


**Script of Scripts (SoS)** consists of [`SoS Notebook`](https://github.com/vatlab/sos-notebook), a Jupyter-based polyglot notebook that allows the use of multiple Jupyter kernels in one notebook, and
[`SoS Workflow`](https://github.com/vatlab/sos), a workflow system for the execution of workflows in both process- and outcome-oriented styles. It is designed for data scientists and bioinformatics who routinely work with scripts in different languages such as bash, Python, R, and SAS. This repository contains the `SoS Workflow` workflow engine.

Please refer to relevant publications [SoS Notebook: An Interactive Multi-Language Data Analysis Environment](https://academic.oup.com/bioinformatics/advance-article/doi/10.1093/bioinformatics/bty405/5001386
) in Bioinformatics, [Script of Scripts: a pragmatic workflow system for daily computational research](https://doi.org/10.1371/journal.pcbi.1006843) in PLoS Computational Biology, and the [SoS homepage](http://vatlab.github.io/sos-docs) for more information on SoS Notebook and SoS Workflow.

We welcome and value community contributions: please [post issues](https://github.com/vatlab/SoS/issues)
to provide us feedback or get our support; please [send pull requests](https://github.com/vatlab/SoS/pulls)
if you have helped fixing bugs or making improvements to the source code. In addition, if you are interesting in extending SoS, for example adding a new target type or task engine, please have a look at our tutorial on extending SoS [here](https://vatlab.github.io/sos-docs/doc/user_guide/extending_sos.html).

# Installing SoS Workflow and Notebook

* If you are using conda, you can install sos using command

  ```bash
  conda install sos -c conda-forge
  ```
  You can also install a majority of the SoS suite of tools using commands such as
  ```bash
  conda install sos sos-pbs sos-notebook jupyterlab-sos sos-bash sos-python sos-r -c conda-forge
  ```
  This will install SoS Workflow, SoS Notebook and its JupyterLab extension, language modules for
  `Bash`, `Python2`, `Python3`, `R`, `R` itself (`r-base` and `r-irkernel` etc) and needed
  libraries (e.g. `feather`) if needed.

* If you are not using conda, you will have to install all the pieces one by one, but you will also have the freedom to use non-conda installation of `R`, `Julia` etc. With a working Python 3.6 installation, you can install SoS Workflow with command

  ```bash
  pip install sos
  ```
  and also
  ```bash
  pip install sos-pbs
  ```
  if you would like to use SoS with a remote batch system such as LSF or Slurm.

  You can install SoS Notebook, and register the sos kernel with Jupyter using the following commands

  ```bash
  pip install sos-notebook
  python -m sos_notebook.install
  ```

  To exchange data between live kernels in SoS Notebook, you will need to install individual kernels (e.g. `irkernel` for `R`), make sure they work under Jupyter, and install SoS language modules using commands such as

  ```bash
  pip install sos-r sos-matlab sos-python sos-bash
  ```
  Different modules might be needed to assist data exchange among kernels. Please refer to [the installation page of sos website](https://vatlab.github.io/sos-docs/#runningsos) for details.

### Change Log of SoS Workflow and SoS Notebook

SoS 0.22.5:
* [sos#1420](https://github.com/vatlab/sos/issues/1420): Allow checking the status, killing, and purge of remote workflows.

SoS Notebook 0.22.3
* [sos-notebook#303](https://github.com/vatlab/sos-notebook/issues/303): Make task execution non-blocking in sos notebook, so that the buttons to check status and remove tasks actually operatable.

SoS Notebook 0.22.2
* [sos-notebook#307](https://github.com/vatlab/sos-notebook/issues/307): Use a new template organization method for nbconvert > 0.6.0.

SoS 0.21.12
* [sos#1392](https://github.com/vatlab/sos/issues/1392): Allow the use of `pem_file` to authenticate to remote hosts.

SoS 0.20.9
* [sos#1319](https://github.com/vatlab/SoS/issues/1319): Extend option `-r host` to allow workflow to be executed with templates and on PBS
* [sos#1322](https://github.com/vatlab/SoS/issues/1322): Deprecate option `-b BIN_DIR` due to [sos#1319](https://github.com/vatlab/SoS/issues/1319)

SoS 0.19.15
* [sos#1273](https://github.com/vatlab/SoS/issues/1273): Allow workflows to be executed on cluster systems.
* [sos#1277](https://github.com/vatlab/SoS/issues/1277): Allow master tasks to be executed on cluster systems with extended runtime option `trunk_workers`.
* [sos#1279](https://github.com/vatlab/SoS/issues/1279): Extend option `-j` of commands `sos run` and `sos execute` to use remote workers.
* [sos#1288](https://github.com/vatlab/SoS/issues/1288): Change the default value of option `-q` from `localhost` to `None` (no task).

SoS 0.19.0
* [sos#1056](https://github.com/vatlab/SoS/issues/1056), [sos#1218](https://github.com/vatlab/SoS/issues/1218): Use a new shared execution model to enforce optio `-j`.
* [sos#1219](https://github.com/vatlab/SoS/issues/1219): Make sure global sections are executed only once

SoS Notebook 0.18.5
* [sos-notebook#192](https://github.com/vatlab/sos-notebook/issues/192): Allow language modules to support pattern matched kernel names such as julia-?.?

SoS 0.18.6
* [sos#1206](https://github.com/vatlab/SoS/issues/1206): Allow `output_from(step)` to obtain output from a workflow.

SoS 0.18.5
* [sos#1186](https://github.com/vatlab/SoS/issues/1186): Add command line option `-T` to enfore dependency tracing.
* [sos#1197](https://github.com/vatlab/SoS/issues/1197): Introduce function `traced` to make dependencies that will always be traced
* [sos#1201](https://github.com/vatlab/SoS/issues/1201): Introduce dependencies between compounded workflows

SoS 0.18.4
* [sos#1185](https://github.com/vatlab/SoS/issues/1185): Disallow the use of parameters in tasks.
* [sos#1186](https://github.com/vatlab/SoS/issues/1186): (reverted) Enforcing the concept that targets in `depends` statement always try to build dependency.

SoS Notebook 0.18.1
* [sos-notebook#178](https://github.com/vatlab/sos-notebook/issues/178): Allow adding `&` to the end of magics `%run`, `%sosrun`, and `%runfile` to execute workflow in background.
* [sos-notebook#179](https://github.com/vatlab/sos-notebook/issues/179): Remove magic `%rerun` and add magic `%runfile`
* [sos-notebook#180](https://github.com/vatlab/sos-notebook/issues/180): Add option `-r` to `%save` to execute the cell after saving it.

SoS 0.18.0
* [sos#1115](https://github.com/vatlab/SoS/issues/1115): Function `output_from` and `named_output` to support named inputs and outputs, among other new features summarized in this ticket.
* [sos#1120](https://github.com/vatlab/SoS/issues/1120): Allow paremeters `group_by`, `paired_with` etc for functions `output_from` and `named_output`.
* [sos#1125](https://github.com/vatlab/SoS/issues/1125): Set `concurrent=True` as default for substep execution.
* [sos#1132](https://github.com/vatlab/SoS/issues/1132): Deprecate action `stop_if` and replace it with `done_if` and `skip_if`
* [sos#1175](https://github.com/vatlab/SoS/issues/1175): Enforce the use of `sos_variable` to import shared variable in a step

SoS Notebook 0.18.0
* [sos-notebook#150](https://github.com/vatlab/sos-notebook/issues/150): A new side panel that works in the same fashion as JupyterLab's console window.
* [sos-notebook#154](https://github.com/vatlab/sos-notebook/issues/154): New task status table that allows reporting status, killing, and purging multiple tasks with the same tags.

SoS 0.9.16.10
* [sos#786](https://github.com/vatlab/SoS/issues/786): Support singularity. See [SoS Singularity Guide](https://vatlab.github.io/sos-docs/doc/tutorials/Singularity.html) for details.

SoS 0.9.16.0, SoS Notebook 0.9.16.0
* [sos#991](https://github.com/vatlab/SoS/issues/991): Use a new task file format to replace multiple files for each task. **This change is not backward compatible so please upgrade only after you completed and removed all existing tasks.**

SoS 0.9.15.1
* [sos-notebook#89](https://github.com/vatlab/sos-notebook/issues/89): Added templates to highlight source code using codemirror, with optional auto-generated table of contents.

SoS 0.9.14.10
* [sos#983](https://github.com/vatlab/SoS/issues/983): Allow depending on an entire process-oriented workflow using a `sos_step()` target that matches multiple steps.

SoS 0.9.14.3:
* [sos#975](https://github.com/vatlab/SoS/issues/975): Add option `-p` to generate a summary report after the completion of workflow.
* [sos#976](https://github.com/vatlab/SoS/issues/976): Much improved workflow help message (`sos run script -h`).

SoS Notebook 0.9.14.4:
* [sos-notebook#79](https://github.com/vatlab/sos-notebook/issues/79): Allow auto-completion and inspection in subkernel cells.

SoS Notebook 0.9.14.1
* [sos-notebook#74](https://github.com/vatlab/sos-notebook/issues/74): Add a `%revisions` magic to display revision history of the current document.

SoS 0.9.14.1
* [sos#925](https://github.com/vatlab/SoS/issues/924): Output summary of executed and ignored step, substeps, and tasks after the execution of workflows.

SoS Notebook 0.9.13.4
* [jupyterlab-sos#11](https://github.com/vatlab/jupyterlab-sos/issues/11): Magic `%cd` now changes directory of all subkernels

SoS Notebook 0.9.12.12
* [sos-notebook#52](https://github.com/vatlab/sos-notebook/issues/52): All new syntax highlighter that highlights expanded expressions
* [sos-notebook#58](https://github.com/vatlab/sos-notebook/issues/58): Stop removing leading comments from cells.

SoS 0.9.12.11
* [sos#922](https://github.com/vatlab/SoS/issues/922): Use user-id for docker execution (-u)
* [sos#926](https://github.com/vatlab/SoS/issues/926): Add function `zap()` to SoS path classes `path`, `paths`, `file_target`, and `sos_targets`


SoS Notebook 0.9.12.11
* [sos-notebook#44](https://github.com/vatlab/sos-notebook/issues/44): Allow sending text in markdown cells to side panel for execution.
* [sos-notebook#47](https://github.com/vatlab/sos-notebook/issues/47): Allow clear any HTML element with magic `%clear --class`
* [sos-notebook#50](https://github.com/vatlab/sos-notebook/issues/50): Re-design logo for SoS Notebook.

SoS 0.9.12.9
* [sos#914](https://github.com/vatlab/SoS/issues/914): Allow option `active` of actions and tasks to accept conditions.
* [sos#915](https://github.com/vatlab/SoS/issues/915): Automatically expand user (`~`) for SoS path types `path`, `paths` and `file_targets`.
* [sos#916](https://github.com/vatlab/SoS/issues/916): Use hashlib instead of faster xxhash under windows

SoS Notebook 0.9.12.9
* [sos-notebook#41](https://github.com/vatlab/sos-notebook/issues/41): Stop saving unused kernels in sos notebook.

SoS 0.9.12.3
* [sos#859](https://github.com/vatlab/SoS/issues/859): Introduce automatic auxiliary steps to simplify the use of makefile steps.

SoS 0.9.11.3
* [sos#879](https://github.com/vatlab/SoS/issues/879): Add action options `stdout` and `stderr` to redict output from script-executing actions.
* [sos-notebook#42](https://github.com/vatlab/sos-notebook/issues/42): Add option `--append` to magic `%capture` .

SoS 0.9.11.2
* [sos-notebook#39](https://github.com/vatlab/sos-notebook/issues/39): Separation installation and deployment and use command `python -m sos_notebook.install` to install `sos` kernel to Jupyter.

SoS 0.9.10.19

* [sos#874](https://github.com/vatlab/SoS/issues/874): Add input option `concurrent=True` to allow parallel execution of input groups.
* [sos#874](https://github.com/vatlab/SoS/issues/874): Optimize task submission of task engines to reduce status checking

SoS Notebook 0.9.10.17

* [sos-notebook#32](https://github.com/vatlab/sos-notebook/issues/32): Add magic `%capture` to capture output of cells
