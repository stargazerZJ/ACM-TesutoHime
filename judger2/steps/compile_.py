__all__ = 'compile', 'ensure_input'

from logging import getLogger
from os import utime
from pathlib import PosixPath
from shutil import copy2, which
from typing import List
from uuid import uuid4

from cache import CachedFile, ensure_cached, upload
from config import cache_dir, cxx, cxxflags, cxx_file_name, cxx_exec_name, \
                   exec_file_name, git_exec_name, verilog, verilog_file_name, \
                   verilog_exec_name
from sandbox import chown_back, run_with_limits
from task_typing import CompileLocalResult, CompileResult, CompileSourceCpp, \
                        CompileSourceGit, CompileSourceVerilog, CompileTask, \
                        Input, InvalidTaskException, ResourceUsage
from util import TempDir, copy_supplementary_files

logger = getLogger(__name__)


async def compile (task: CompileTask) -> CompileLocalResult:
    with TempDir() as cwd:
        # copy supplementary files
        await copy_supplementary_files(task.supplementary_files, cwd)

        # compile
        type = task.source.type
        # The compiled program returned by these functions
        # reside inside cwd. As cwd is deleted once this
        # function returns, the file need to be uploaded or
        # cached for later use, and the cache location is
        # returned as a semi-persistent compile artifact
        # location.
        if type == 'cpp':
            res = await compile_cpp(cwd, task.source, task.limits)
        elif type == 'git':
            res = await compile_git(cwd, task.source, task.limits)
        elif type == 'verilog':
            res = await compile_verilog(cwd, task.source, task.limits)
        else:
            raise InvalidTaskException(f'compile: invalid source type {type}')

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


class NotCompiledException (Exception): pass

async def ensure_input (input: Input) -> CachedFile:
    if input.type == 'compile':
        res = await compile(input)
        if res.result.result != 'compiled':
            raise NotCompiledException(res.result.message)
        return CachedFile(res.local_path, exec_file_name)
    return ensure_cached(input.url)


async def compile_cpp (
    cwd: PosixPath,
    source: CompileSourceCpp,
    limits: ResourceUsage,
) -> CompileLocalResult:
    main_file = await ensure_cached(source.main)
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
    return CompileLocalResult.from_file(exec_file)


async def compile_git (
    cwd: PosixPath,
    source: CompileSourceGit,
    limits: ResourceUsage,
) -> CompileLocalResult:
    logger.debug(f'about to compile git repo {repr(source.url)}')

    def run_build_step (argv: List[str], network = False):
        bind = ['/bin', '/usr/bin', '/usr/include', '/usr/share/cmake']
        return run_with_limits(
            argv, cwd, limits,
            supplementary_paths=bind,
            network_access=network,
        )

    # clone
    git_argv = [which('git'), 'clone', source.url, '.', '--depth', '1']
    logger.debug(f'about to run {git_argv}')
    clone_res = await run_build_step(git_argv, True)
    if clone_res.error != None:
        return CompileLocalResult.from_run_failure(clone_res)

    # configure
    cmake_lists_path = cwd / 'CMakeLists.txt'
    if cmake_lists_path.is_file():
        logger.debug('CMake config found, invoking cmake')
        res = await run_build_step([which('cmake'), '.'])
        if res.error != None:
            return CompileLocalResult.from_run_failure(res)

    # compile
    makefile_paths = [cwd / x for x in ['GNUmakefile', 'makefile', 'Makefile']]
    if any(x.is_file() for x in makefile_paths):
        logger.debug('makefile found, invoking make')
        res = await run_build_step([which('make')])
        if res.error != None:
            return CompileLocalResult.from_run_failure(res)

    # check
    exe = cwd / git_exec_name
    if not exe.is_file():
        msg = f'Executable \'{git_exec_name}\' not found in built files;' \
            f'please ensure your compile output is named \'{git_exec_name}\'' \
            'in the root directory of the repository.'
        return CompileLocalResult(
            CompileResult('runtime_error', msg),
            None,
        )

    # done
    return CompileLocalResult.from_file(exe)


async def compile_verilog (
    cwd: PosixPath,
    source: CompileSourceVerilog,
    limits: ResourceUsage,
) -> CompileLocalResult:
    main_file = await ensure_cached(source.main)
    code_file = cwd / verilog_file_name
    exec_file = cwd / verilog_exec_name
    copy2(main_file, code_file)
    res = await run_with_limits(
        [verilog, str(code_file), '-o', str(exec_file)],
        cwd, limits,
        supplementary_paths=['/bin', '/usr/bin'],
    )
    if res.error != None:
        return CompileLocalResult.from_run_failure(res)
    return CompileLocalResult.from_file(exec_file)
