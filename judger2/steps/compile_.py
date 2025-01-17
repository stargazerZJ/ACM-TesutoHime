__all__ = 'compile', 'ensure_input'

from logging import getLogger
from os import utime
from pathlib import PosixPath
from shutil import copy2, which
from subprocess import DEVNULL, PIPE
from typing import Any, Callable, Coroutine, Dict, List
from uuid import uuid4

from commons.task_typing import (CompileLocalResult, CompileResult,
                                 CompileSource, CompileSourceCpp,
                                 CompileSourceGit, CompileSourceVerilog,
                                 CompileTask, Input, ResourceUsage)

from judger2.cache import CachedFile, ensure_cached, upload
from judger2.config import (cache_dir, cxx, cxx_exec_name, cxx_file_name,
                            cxxflags, exec_file_name, git_exec_name, gitflags,
                            verilog, verilog_exec_name, verilog_file_name)
from judger2.sandbox import chown_back, run_with_limits
from judger2.util import TempDir, copy_supplementary_files

logger = getLogger(__name__)


async def compile(task: CompileTask) -> CompileLocalResult:
    with TempDir() as cwd:
        # copy supplementary files
        await copy_supplementary_files(task.supplementary_files, cwd)

        # compile
        type = task.source.__class__

        # The compiled program returned by these functions
        # reside inside cwd. As cwd is deleted once this
        # function returns, the file need to be uploaded or
        # cached for later use, and the cache location is
        # returned as a semi-persistent compile artifact
        # location.
        res = await compilers[type](cwd, task.source, task.limits)

        # check for errors
        if res.result.result != 'compiled':
            return res
        if res.local_path == None:
            return CompileLocalResult(
                result=CompileResult(
                    result='system_error',
                    message='compilation succeeded without artifact'
                ),
                local_path=None
            )
        # Chown back, or else we probably couldn't copy the
        # artifact in case the file is of mode like 0700
        # set by the compiler or by a bad mask.
        chown_back(cwd)

        # upload artifacts
        if task.artifact != None:
            local_path = (await upload(res.local_path, task.artifact.url)).path
        else:
            local_path = PosixPath(cache_dir) / str(uuid4())
            copy2(res.local_path, local_path)
            # touch the local file so the file will be eventually deleted
            utime(local_path)

        # done
        return CompileLocalResult(result=res.result, local_path=local_path)


class NotCompiledException(Exception): pass

async def ensure_input(input: Input) -> CachedFile:
    if isinstance(input, CompileTask):
        res = await compile(input)
        if res.result.result != 'compiled':
            raise NotCompiledException(res.result.message)
        return CachedFile(res.local_path, exec_file_name)
    return await ensure_cached(input.url)


async def compile_cpp(
    cwd: PosixPath,
    source: CompileSourceCpp,
    limits: ResourceUsage,
) -> CompileLocalResult:
    main_file = (await ensure_cached(source.main)).path
    code_file = cwd / cxx_file_name
    exec_file = cwd / cxx_exec_name
    copy2(main_file, code_file)
    res = await run_with_limits(
        [cxx] + cxxflags + [str(code_file), '-o', str(exec_file)],
        cwd, limits,
        supplementary_paths=['/bin', '/usr/bin', '/usr/include'],
    )
    if res.error != None:
        return CompileLocalResult.from_run_failure(res)
    return CompileLocalResult.from_file(exec_file, res.message)


def get_resolv_conf_path():
    resolv_conf = PosixPath('/etc/resolv.conf')
    if not resolv_conf.exists():
        return None
    if not resolv_conf.is_symlink():
        return None
    return str(resolv_conf.resolve())

resolv_conf_path = get_resolv_conf_path()

async def compile_git(
    cwd: PosixPath,
    source: CompileSourceGit,
    limits: ResourceUsage,
) -> CompileLocalResult:
    logger.debug(f'about to compile git repo {repr(source.url)}')

    def run_build_step(argv: List[str], network = False, output = DEVNULL):
        bind = ['/bin', '/usr/bin', '/usr/include', '/usr/share', '/etc']
        if resolv_conf_path is not None:
            bind.append(resolv_conf_path)
        return run_with_limits(
            argv, cwd, limits,
            outfile=output,
            supplementary_paths=bind,
            network_access=network,
            env=[
                'GIT_CONFIG_COUNT=1',
                'GIT_CONFIG_KEY_0=safe.directory',
                'GIT_CONFIG_VALUE_0=*',
            ],
        )

    # clone
    git_argv = [which('git'), 'clone', source.url, '.'] + gitflags
    logger.debug(f'about to run {git_argv}')
    clone_res = await run_build_step(git_argv, True)
    if clone_res.error != None:
        return CompileLocalResult.from_run_failure(clone_res)

    # get commit hash
    with TempDir() as d, open(d / 'commit-hash', 'w+b') as ouf:
        commit_hash_argv = [which('git'), 'log', '-1', '--pretty=%H']
        commit_hash_res = await run_build_step(commit_hash_argv, False, ouf)
        if commit_hash_res.error != None:
            return CompileLocalResult.from_run_failure(commit_hash_res)
        ouf.seek(0)
        commit_hash = ouf.read(128).decode().strip()
        logger.debug(f'git commit hash: {commit_hash}')
        message = f'Using git commit {commit_hash}'

    # configure
    cmake_lists_path = cwd / 'CMakeLists.txt'
    if cmake_lists_path.is_file():
        logger.debug('CMake config found, invoking cmake')
        res = await run_build_step([which('cmake'), '.'])
        if res.error != None:
            return CompileLocalResult.from_run_failure(res)
    else:
        message += '\nWarning: CMakeLists.txt not found, skipping cmake invocation'

    # compile
    makefile_paths = [cwd / x for x in ['GNUmakefile', 'makefile', 'Makefile']]
    if any(x.is_file() for x in makefile_paths):
        logger.debug('makefile found, invoking make')
        res = await run_build_step([which('make')])
        if res.error != None:
            return CompileLocalResult.from_run_failure(res)
    else:
        message += '\nWarning: Makefile not found, skipping make invocation'

    # check
    exe = cwd / git_exec_name
    if not exe.is_file():
        msg = message + '\n' + \
            f'Executable \'{git_exec_name}\' not found in built files; ' \
            f'please ensure your compile output is named \'{git_exec_name}\' ' \
            'in the root directory of the repository.'
        return CompileLocalResult(
            CompileResult('runtime_error', msg),
            None,
        )

    # done
    return CompileLocalResult.from_file(exe, message)


async def compile_verilog(
    cwd: PosixPath,
    source: CompileSourceVerilog,
    limits: ResourceUsage,
) -> CompileLocalResult:
    main_file = (await ensure_cached(source.main)).path
    code_file = cwd / verilog_file_name
    exec_file = cwd / verilog_exec_name
    copy2(main_file, code_file)
    res = await run_with_limits(
        [verilog, str(code_file), '-o', str(exec_file)],
        cwd, limits,
        supplementary_paths=['/bin', '/usr/bin'],
        tmpfsmount=True,
    )
    if res.error != None:
        return CompileLocalResult.from_run_failure(res)
    return CompileLocalResult.from_file(exec_file)


Compiler = Callable[
    [PosixPath, CompileSource, ResourceUsage],
    Coroutine[Any, Any, CompileLocalResult],
]
compilers: Dict[Any, Compiler] = {
    CompileSourceCpp: compile_cpp,
    CompileSourceGit: compile_git,
    CompileSourceVerilog: compile_verilog,
}
