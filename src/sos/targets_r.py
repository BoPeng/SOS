#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os

from sos.targets import BaseTarget
from sos.utils import env, textMD5
import shutil


class R_library(BaseTarget):
    """A target for a R library."""

    LIB_STATUS_CACHE = {}

    def __init__(
        self,
        library,
        version=None,
        repos="http://cran.us.r-project.org",
        autoinstall=False,
    ):
        super(R_library, self).__init__()
        self._library = library
        if version is not None:
            version = (version,) if isinstance(version, str) else tuple(version)
        self._version = version
        self._repos = repos
        self._autoinstall = autoinstall

    def _install(self, name, version, repos):
        """Check existence and version match of R library.
        cran and bioc packages are unique yet might overlap with github.
        Therefore if the input name is {repo}/{pkg} the package will be
        installed from github if not available, else from cran or bioc
        """
        from sos.pattern import glob_wildcards
        import tempfile
        import subprocess

        output_file = tempfile.NamedTemporaryFile(
            mode="w+t", suffix=".txt", delete=False
        ).name
        script_file = tempfile.NamedTemporaryFile(
            mode="w+t", suffix=".R", delete=False
        ).name
        #
        package_loaded = (
            "suppressMessages(require(package, character.only=TRUE, quietly=TRUE))"
        )
        version_satisfied = "TRUE"
        for opt in ("==", ">=", ">", "<=", "<", "!="):
            if opt in name:
                if version is not None:
                    raise ValueError(
                        f"Specifying 'version=' option in addition to '{name}' is not allowed"
                    )
                name, version = [x.strip() for x in name.split(opt, 1)]
                if "," in version:
                    raise ValueError(
                        f"SoS does not yet support multiple version comparisons. {version} provided"
                    )
                version = (opt + version,)
                break
        if version is not None:
            version = list(version)
            operators = []
            for idx, value in enumerate(version):
                value = str(value)
                if value.startswith(">="):
                    operators.append(">=")
                    version[idx] = value[2:]
                elif value.startswith(">"):
                    operators.append(">")
                    version[idx] = value[1:]
                elif value.startswith("<="):
                    operators.append("<=")
                    version[idx] = value[2:]
                elif value.startswith("<"):
                    operators.append("<")
                    version[idx] = value[1:]
                elif value.startswith("=="):
                    operators.append("==")
                    version[idx] = value[2:]
                elif value.startswith("!="):
                    operators.append("!=")
                    version[idx] = value[2:]
                else:
                    operators.append("==")
            # check version and mark version mismatch
            # if current version satisfies any of the
            # requirement the check program quits
            version_satisfied = "||".join(
                [f"(cur_version {y} {repr(x)})" for x, y in zip(version, operators)]
            )
        #
        if len(glob_wildcards("{repo}@{pkg}", [name])["repo"]):
            # package is from github
            self._install("remotes>=2.0.0", None, repos)
            install_script = f"""
            options(warn=-1)
            package_repo <-strsplit("{name}", split="@")[[1]][2]
            package <-strsplit("{name}", split="@")[[1]][1]
            if ({package_loaded}) cur_version <- packageVersion(package) else cur_version <- NULL
            if (!is.null(cur_version) && {version_satisfied}) {{
                write(paste(package, cur_version, "AVAILABLE"), file={repr(output_file)})
            }} else if ({"TRUE" if self._autoinstall else "FALSE"}) {{
                remotes::install_github(package_repo, force=TRUE, upgrade="never")
                if ({package_loaded}) cur_version <- packageVersion(package) else cur_version <- NULL
                # if it still does not exist, write the package name to output
                if (!is.null(cur_version)) {{
                    if ({version_satisfied}) write(paste(package, cur_version, "INSTALLED"), file={repr(output_file)})
                    else write(paste(package, cur_version, "VERSION_MISMATCH"), file={repr(output_file)})
                }} else {{
                    write(paste(package, "NA", "MISSING"), file={repr(output_file)})
                    quit("no")
                }}
            }} else {{
                if (!is.null(cur_version)) write(paste(package, cur_version, "VERSION_MISMATCH"), file={repr(output_file)}) else write(paste(package, cur_version, "UNAVAILABLE"), file={repr(output_file)})
            }}
            """
        else:
            # package is from cran or bioc
            install_script = f"""
            options(warn=-1)
            package <- "{name}"
            if ({package_loaded}) cur_version <- packageVersion(package) else cur_version <- NULL
            if (!is.null(cur_version) && {version_satisfied}) {{
                write(paste(package, cur_version, "AVAILABLE"), file={repr(output_file)})
            }} else if ({"TRUE" if self._autoinstall else "FALSE"}) {{
                install.packages(package, repos="{repos}", quiet=FALSE)
                # if the package still does not exist
                if (!{package_loaded}) {{
                    source("http://bioconductor.org/biocLite.R")
                    biocLite(package, suppressUpdates=TRUE, suppressAutoUpdate=TRUE, ask=FALSE)
                }}
                if ({package_loaded}) cur_version <- packageVersion(package) else cur_version <- NULL
                # if it still does not exist, write the package name to output
                if (!is.null(cur_version)) {{
                    if ({version_satisfied}) write(paste(package, cur_version, "INSTALLED"), file={repr(output_file)}) else write(paste(package, cur_version, "VERSION_MISMATCH"), file={repr(output_file)})
                }} else {{
                    write(paste(package, "NA", "MISSING"), file={repr(output_file)})
                    quit("no")
                }}
            }} else {{
                if (!is.null(cur_version)) write(paste(package, cur_version, "VERSION_MISMATCH"), file={repr(output_file)}) else write(paste(package, cur_version, "UNAVAILABLE"), file={repr(output_file)})
            }}
            """
        # temporarily change the run mode to run to execute script
        try:
            with open(script_file, "w") as sfile:
                sfile.write(install_script)
            #
            p = subprocess.Popen(["Rscript", "--default-packages=utils", script_file])
            ret = p.wait()
            if ret != 0:
                env.logger.warning(f"Failed to detect or install R library {name}")
                return False
        except Exception as e:
            env.logger.error(f"Failed to execute script: {e}")
            return False
        finally:
            os.remove(script_file)

        ret_val = False
        with open(output_file) as tmp:
            for line in tmp:
                lib, cur_version, status = line.split(" ", 2)
                if status.strip() == "MISSING":
                    env.logger.error(
                        f"R library {lib} is not available and cannot be installed."
                    )
                elif status.strip() == "UNAVAILABLE":
                    env.logger.error(f"R library {lib} is not available.")
                elif status.strip() == "AVAILABLE":
                    env.logger.debug(f"R library {lib} ({cur_version}) is available")
                    ret_val = True
                elif status.strip() == "INSTALLED":
                    env.logger.debug(
                        f"R library {lib} ({cur_version}) has been installed"
                    )
                    ret_val = True
                elif status.strip() == "VERSION_MISMATCH":
                    env.logger.error(
                        f'R library {lib} ({cur_version}) does not satisfy version requirement ({"/".join(version)})!'
                    )
                else:
                    raise RuntimeError(f"This should not happen: {line}")
        try:
            os.remove(output_file)
        except Exception:
            pass
        return ret_val

    def target_exists(self, mode="any"):
        if (self._library, self._version, self._autoinstall) in self.LIB_STATUS_CACHE:
            return self.LIB_STATUS_CACHE[
                (self._library, self._version, self._autoinstall)
            ]
        else:
            # check if R is installed
            if not shutil.which("Rscript"):
                env.logger.debug(
                    f'Target R_Library("{self._library}") does not exist because command Rscript is not found.'
                )
                return False
            ret = self._install(self._library, self._version, self._repos)
            self.LIB_STATUS_CACHE[
                (self._library, self._version, self._autoinstall)
            ] = ret
            return ret

    def target_name(self):
        return self._library

    def __repr__(self):
        if self._version:
            return (
                f'{self.__class__.__name__}("{self.target_name()}", {self._version!r})'
            )
        else:
            return super(R_library, self).__repr__()

    def target_signature(self, mode="any"):
        # we are supposed to get signature of the library, but we cannot
        return textMD5(repr(self._library))
