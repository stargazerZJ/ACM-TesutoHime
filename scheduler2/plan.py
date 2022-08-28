__all__ = 'generate_plan', 'execute_plan'

import json
from asyncio import FIRST_COMPLETED, Task, create_task, wait
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum, auto
from logging import getLogger
from os import remove
from pathlib import PosixPath
from typing import Dict, Iterable, List, Literal, Optional, Set, Tuple
from zipfile import ZipFile

from commons.task_typing import (Artifact, CodeLanguage, CompareChecker,
                                 CompileResult, CompileSource,
                                 CompileSourceCpp, CompileSourceGit,
                                 CompileSourceVerilog, CompileTask,
                                 CompileTaskPlan, DirectChecker, FileUrl,
                                 GroupJudgeResult, JudgePlan, JudgeResult,
                                 JudgeTask, JudgeTaskPlan, ProblemJudgeResult,
                                 ResourceUsage, ResultType, RunArgs,
                                 SpjChecker, Testpoint, TestpointGroup,
                                 TestpointJudgeResult, UserCode)

from scheduler2.config import (default_check_limits, default_compile_limits,
                               default_run_limits, problem_config_filename,
                               s3_buckets, working_dir)
from scheduler2.dispatch import run_task
from scheduler2.problem_typing import Group, ProblemConfig, Spj
from scheduler2.problem_typing import Testpoint as ConfigTestpoint
from scheduler2.s3 import (download, remove_file, sign_url_get, sign_url_put, upload,
                           upload_obj)

logger = getLogger(__name__)


class InvalidProblemException (Exception): pass


url_scheme = 's3://'

@dataclass
class ParseContext:
    problem_id: str
    zip: ZipFile
    cfg: Optional[ProblemConfig] = None
    testpoint_count: Optional[int] = None
    compile_type: Literal['classic', 'hpp', 'hpp-per-testpoint', 'none'] = None
    check_type: Literal['compare', 'direct', 'spj'] = None
    compile_limits: Optional[ResourceUsage] = None
    compile_supp: Optional[List[str]] = None
    files_to_upload: Set[str] = field(default_factory=lambda: set())
    checker_to_compile: Optional[str] = None
    checker_artifact: Optional[Artifact] = None
    plan: JudgePlan = field(default_factory=lambda: JudgePlan())

    def file_key (self, filename: str):
        return f'{self.problem_id}/{filename}'

    def file_url (self, filename: str) -> FileUrl:
        self.files_to_upload.add(filename)
        return f'{url_scheme}{self.file_key(filename)}'


def sign_url (url: str):
    if not url.startswith(url_scheme):
        raise InvalidProblemException(f'Invalid object url {url}')
    return sign_url_get(s3_buckets.problems, url.replace(url_scheme, '', 1))


async def load_config (ctx: ParseContext):
    try:
        with ctx.zip.open(problem_config_filename, 'r') as f:
            cfg = json.load(f)
    except BaseException as e:
        msg = f'cannot read {problem_config_filename}: {e}'
        raise InvalidProblemException(msg)
    try:
        cfg['Groups'] = [Group(**x) for x in cfg['Groups']]
        cfg['Details'] = [ConfigTestpoint(**x) for x in cfg['Details']]
        ctx.cfg = ProblemConfig(**cfg)
        ctx.testpoint_count = len(ctx.cfg.Details)
    except BaseException as e:
        raise InvalidProblemException(str(e))


checker_source_filename = 'spj.cpp'
checker_precompiled_filename = 'spj_bin'
checker_exec_filename = 'checker'

async def parse_spj (ctx: ParseContext):
    spj = ctx.cfg.SPJ
    type_map = {
        Spj.CLASSIC_COMPARE: ('classic', 'compare'),
        Spj.CLASSIC_SPJ: ('classic', 'spj'),
        Spj.HPP_DIRECT: ('hpp', 'direct'),
        Spj.HPP_COMPARE: ('hpp', 'compare'),
        Spj.HPP_SPJ: ('hpp', 'spj'),
        Spj.NONE_SPJ: ('none', 'spj'),
    }
    if not spj in type_map:
        raise InvalidProblemException(f'Invalid SPJ type {spj}')
    ctx.compile_type, ctx.check_type = type_map[spj]
    if ctx.cfg.Scorer != 0:
        raise InvalidProblemException(f'Scorers are not supported (yet)')
    if ctx.check_type == 'checker':
        if checker_precompiled_filename in ctx.zip.namelist():
            ctx.checker_artifact = Artifact(
                ctx.file_url(checker_precompiled_filename))
        elif checker_source_filename in ctx.zip.namelist():
            ctx.checker_artifact = Artifact(
                f'{url_scheme}{ctx.file_key(checker_exec_filename)}')
            ctx.checker_to_compile = ctx.file_url(checker_source_filename)
        else:
            raise InvalidProblemException(f'{checker_source_filename} not found')


hpp_main_filename = 'main.cpp'
hpp_main_template = '{}.cpp'
hpp_main_template_vlog = '{}.v'
hpp_src_filename = 'src.hpp'
hpp_src_filename_vlog = 'answer.v'

async def parse_compile (ctx: ParseContext) -> Optional[CompileTaskPlan]:
    limits = deepcopy(default_compile_limits)
    time_msecs = ctx.cfg.CompileTimeLimit
    if time_msecs is not None:
        limits.time_msecs = time_msecs
    ctx.compile_limits = limits

    supplementary_files = list(map(ctx.file_url, ctx.cfg.SupportedFiles))
    ctx.compile_supp = supplementary_files

    if ctx.compile_type == 'none':
        return None

    task = CompileTaskPlan(
        source=UserCode(),
        supplementary_files=supplementary_files,
        artifact=True,
        limits=limits,
    )
    if ctx.compile_type == 'classic':
        return task
    src_filename = hpp_src_filename_vlog if ctx.cfg.Verilog \
        else hpp_src_filename
    task.supplementary_files.append(UserCode(src_filename))

    has_main = hpp_main_filename in ctx.zip.namelist()
    if has_main:
        task.source = CompileSourceCpp(ctx.file_url(hpp_main_filename))
        return task
    ctx.compile_type = 'hpp-per-testpoint'
    task.source = None
    task.artifact = False
    return task


def generate_dependents (plan: List[JudgeTaskPlan]) \
    -> List[JudgeTaskPlan]:
    for i, deps in enumerate(x.dependencies for x in plan):
        for dep in deps:
            plan[dep].dependents.append(i)
    return plan

infile_name_template = '{}.in'
answer_name_template = '{}.ans'
answer_name_template_alt = '{}.out'

def parse_testpoint (ctx: ParseContext, conf: ConfigTestpoint) -> Testpoint:
    id = str(conf.ID)
    infile_name = infile_name_template.format(id)
    infile = ctx.file_url(infile_name)

    run_limits = deepcopy(default_run_limits)
    if conf.TimeLimit is not None: run_limits.time_msecs = conf.TimeLimit
    if conf.MemoryLimit is not None: run_limits.memory_bytes = conf.MemoryLimit
    if conf.DiskLimit is not None: run_limits.file_size_bytes = abs(conf.DiskLimit)
    if conf.FileNumberLimit is not None: run_limits.file_count = conf.FileNumberLimit

    type = 'verilog' if ctx.cfg.Verilog else \
        'valgrind' if conf.ValgrindTestOn else 'elf'
    run = None if ctx.check_type == 'direct' else RunArgs(
        type=type,
        limits=run_limits,
        infile=infile,
        supplementary_files=[],
    )

    def ans () -> Optional[FileUrl]:
        ans_filename = answer_name_template.format(id)
        if ans_filename in ctx.zip.namelist():
            return ctx.file_url(ans_filename)
        ans_filename_alt = answer_name_template_alt.format(id)
        if ans_filename_alt in ctx.zip.namelist():
            return ctx.file_url(ans_filename_alt)
        return None
    if ctx.check_type == 'compare':
        check = CompareChecker(True, ans())
        if check.answer is None:
            raise InvalidProblemException(f'Answer file needed')
    elif ctx.check_type == 'direct':
        check = DirectChecker()
    elif ctx.check_type == 'spj':
        check = SpjChecker(
            format='checker',
            executable=ctx.checker_artifact,
            answer=ans(),
            supplementary_files=[],
            limits=default_check_limits,
        )
    else:
        raise InvalidProblemException(f'Unknown check type {ctx.check_type}')

    testpoint = Testpoint(
        id=id,
        dependent_on=None if conf.Dependency == 0 else str(conf.Dependency),
        input=UserCode(), 
        run=run, 
        check=check,
    )
    if ctx.compile_type == 'hpp-per-testpoint':
        main_template = hpp_main_template_vlog if ctx.cfg.Verilog \
            else hpp_main_template
        task = deepcopy(ctx.plan.compile)
        task.source = CompileSourceCpp(ctx.file_url(main_template.format(id)))
        testpoint.input = task
    return testpoint

async def parse_testpoints (ctx: ParseContext) -> List[JudgeTaskPlan]:
    testpoints = [parse_testpoint(ctx, x) for x in ctx.cfg.Details]
    testpoints_map = dict((tp.id, tp) for tp in testpoints)
    if ctx.compile_type == 'hpp-per-testpoint':
        ctx.plan.compile = None

    if any(x.DiskLimit is not None and x.DiskLimit > 0 for x in ctx.cfg.Details):
        if ctx.compile_type == 'hpp-per-testpoint':
            msg = 'Per-testpoint compilation is incompatible with persistence testing'
            raise InvalidProblemException(msg)
        plan: List[JudgeTaskPlan] = []
        for testpoint, conf in zip(testpoints, ctx.cfg.Details):
            if len(plan) == 0 or conf.DiskLimit < 0:
                plan.append(JudgeTaskPlan(
                    task=JudgeTask([]),
                    dependencies=[],
                    dependents=[],
                ))
            current_task = plan[-1]
            current_task.task.testpoints.append(testpoint)
            if testpoint.dependent_on is not None:
                msg = f'Invalid dep {conf.Dependency} declared by {conf.ID}'
                if not testpoint.dependent_on in testpoints_map:
                    raise InvalidProblemException(msg)
                dep = testpoints_map[testpoint.dependent_on]
                if all(x is not dep for x in current_task.task.testpoints):
                    for i, group in enumerate(plan):
                        if any(x is dep for x in group.task.testpoints):
                            current_task.dependencies.append(i)
                            break
                    else:
                        raise InvalidProblemException(msg)
        return generate_dependents(plan)

    plan = []
    for testpoint, conf in zip(testpoints, ctx.cfg.Details):
        if testpoint.dependent_on is not None:
            if not testpoint.dependent_on in testpoints_map:
                msg = f'Invalid dep {conf.Dependency} declared by {conf.ID}'
                raise InvalidProblemException(msg)
            dep = testpoints.index(testpoints_map[testpoint.dependent_on])
            dep = [dep]
        else:
            dep = []
        plan.append(JudgeTaskPlan(
            task=JudgeTask([testpoint]),
            dependencies=dep,
            dependents=[],
        ))
    return generate_dependents(plan)


group_name_template = 'Task %d'

async def parse_groups (ctx: ParseContext) -> List[TestpointGroup]:
    return [TestpointGroup(
        name=conf.GroupName if conf.GroupName is not None \
            else group_name_template % (i + 1),
        testpoints=[str(x) for x in conf.TestPoints],
        score=float(conf.GroupScore),
    ) for i, conf in enumerate(ctx.cfg.Groups)]


async def upload_files (ctx: ParseContext):
    for file in ctx.files_to_upload:
        with ctx.zip.open(file, 'r') as f:
            await upload_obj(s3_buckets.problems, ctx.file_key(file), f)


async def compile_checker (ctx: ParseContext):
    source = ctx.checker_to_compile
    if source is None:
        return
    task = CompileTask(
        source=CompileSourceCpp(sign_url(source)),
        supplementary_files=list(map(sign_url, ctx.compile_supp)),
        artifact=sign_url_put(s3_buckets.problems,
            ctx.file_key(checker_exec_filename)),
        limits=ctx.compile_limits,
    )
    res = await run_task(task)
    if res.result != 'compiled':
        msg = f'Cannot compile checker ({res.result}): {res.message}'
        raise InvalidProblemException(msg)


async def generate_plan (problem_id: str) -> JudgePlan:
    zip_filename = f'{problem_id}.zip'
    zip_path = working_dir / zip_filename
    try:
        await download(s3_buckets.problems, zip_filename, zip_path)
        with ZipFile(zip_path) as zip:
            ctx = ParseContext(problem_id, zip)
            await load_config(ctx)
            await parse_spj(ctx)
            ctx.plan.compile = await parse_compile(ctx)
            ctx.plan.judge = await parse_testpoints(ctx)
            ctx.plan.score = await parse_groups(ctx)
            await upload_files(ctx)
            await compile_checker(ctx)
            return ctx.plan
    finally:
        try:
            remove(zip_path)
        except BaseException as e:
            logger.error(f'cannot remove problem zip: {e}')


class InvalidCodeException (Exception): pass


class UrlType (Enum):
    CODE = auto()
    ARTIFACT = auto()

class DependencyNotSatisfied: pass

@dataclass
class JudgeTaskRecord:
    task: JudgeTask
    plan: JudgeTaskPlan
    result: Optional[JudgeResult] = None

@dataclass
class ExecutionContext:
    plan: JudgePlan
    id: str
    lang: CodeLanguage
    code: PosixPath
    compile: Optional[CompileTask] = None
    judge: Optional[List[JudgeTaskRecord]] = None
    code_key: Optional[str] = None
    compile_artifact: Optional[Artifact] = None
    files_to_clean: Set[str] = field(default_factory=lambda: set())

    def file_url (self, type: UrlType, filename: str):
        key = f'{self.id}/{filename}'
        if type == UrlType.CODE:
            if self.code_key is not None \
            and self.code_key != key:
                msg = 'Trying to use user code as different files'
                raise InvalidProblemException(msg)
            self.code_key = key

            return sign_url_get(s3_buckets.submissions, key)
        elif type == UrlType.ARTIFACT:
            if self.compile_artifact is not None:
                raise InvalidProblemException('Duplicate compile artifact')
            bucket = s3_buckets.artifacts
            self.files_to_clean.add((bucket, key))
            self.compile_artifact = Artifact(sign_url_get(bucket, key))
            return sign_url_put(bucket, key)
        else:
            raise Exception(f'Invalid url type {type}')

    def dependencies_satisfied (self, rec: JudgeTaskRecord) -> bool:
        def dependency_satisfied (testpoint: Testpoint) -> bool:
            dep = testpoint.dependent_on
            return dep is None or isinstance(dep, DependencyNotSatisfied) \
                or any(dep == tp.id for tp in rec.task.testpoints)
        return all(map(dependency_satisfied, rec.plan.task.testpoints))


raw_code_filename = 'code'
cpp_main_filename = 'main.cpp'
verilog_main_filename = 'main.v'
artifact_filename = 'main'

def get_compile_source (ctx: ExecutionContext, filename: str) -> CompileSource:
    if ctx.lang == CodeLanguage.CPP:
        return CompileSourceCpp(ctx.file_url(UrlType.CODE, filename))
    if ctx.lang == CodeLanguage.GIT:
        st = ctx.code.stat()
        if st.st_size > 1024:
            raise InvalidCodeException('URL too long')
        return CompileSourceGit(ctx.code.read_text().strip())
    if ctx.lang == CodeLanguage.VERILOG:
        return CompileSourceVerilog(ctx.file_url(UrlType.CODE, filename))
    raise InvalidCodeException('Unknown language')

def prepare_compile_task (ctx: ExecutionContext) -> CompileTask:
    plan = ctx.plan.compile
    if not plan:
        return None

    user_codes = list(filter(lambda x: isinstance(x, UserCode), 
        [plan.source] + plan.supplementary_files))
    if len(user_codes) == 0:
        raise InvalidProblemException('Compile task with no user input')
    if len(user_codes) > 1:
        raise InvalidCodeException('Compile task with multiple files as user input')

    fallback_filename = cpp_main_filename if ctx.lang == CodeLanguage.CPP \
        else verilog_main_filename if ctx.lang == CodeLanguage.VERILOG \
        else None
    filename = user_codes[0].filename
    if filename is None: filename = fallback_filename
    if isinstance(plan.source, UserCode):
        source = get_compile_source(ctx, filename)
    else:
        source = deepcopy(plan.source)
        if source.type == 'cpp' or source.type == 'verilog':
            source.main = sign_url(source.main)
    def map_supplementary_file (file):
        if isinstance(file, UserCode):
            return ctx.file_url(UrlType.CODE, filename)
        else:
            return sign_url(file)
    supplementary_files = [map_supplementary_file(x) for x in
        plan.supplementary_files]

    artifact = Artifact(ctx.file_url(UrlType.ARTIFACT, artifact_filename)) \
        if plan.artifact else None
    limits = deepcopy(plan.limits)
    return CompileTask(source, supplementary_files, artifact, limits)


def get_judge_task (ctx: ExecutionContext, plan: JudgeTaskPlan) \
    -> JudgeTaskRecord:
    task = deepcopy(plan.task)
    for testpoint in task.testpoints:
        if testpoint.dependent_on is not None:
            if all(x.id != testpoint.dependent_on for x in task.testpoints):
                testpoint.dependent_on = None

        match testpoint.input:
            case UserCode():
                if ctx.compile_artifact is None:
                    ctx.compile_artifact = Artifact(ctx.file_url(UrlType.CODE,
                        raw_code_filename))
                testpoint.input = ctx.compile_artifact
            case CompileTaskPlan():
                testpoint.input = prepare_compile_task(ctx, testpoint.input)
            case CompileTask():
                pass
            case Artifact():
                testpoint.input.url = sign_url(testpoint.input.url)

        if testpoint.run is not None:
            if testpoint.run.infile is not None:
                testpoint.run.infile = sign_url(testpoint.run.infile)
            testpoint.run.supplementary_files = \
                [sign_url(x) for x in testpoint.run.supplementary_files]

        match testpoint.check:
            case CompareChecker():
                testpoint.check.answer = sign_url(testpoint.check.answer)
            case DirectChecker():
                pass
            case SpjChecker():
                if testpoint.check.answer is not None:
                    testpoint.check.answer = sign_url(testpoint.check.answer)
                if not isinstance(testpoint.check.executable, Artifact):
                    raise InvalidProblemException('Invalid SPJ')
                testpoint.check.executable.url = \
                    sign_url(testpoint.check.executable.url)
                testpoint.check.supplementary_files = \
                    [sign_url(x) for x in testpoint.check.supplementary_files]

    return JudgeTaskRecord(task, deepcopy(plan))

def get_judge_tasks (ctx: ExecutionContext) -> List[JudgeTaskRecord]:
    return [get_judge_task(ctx, plan) for plan in ctx.plan.judge]


async def upload_code (ctx: ExecutionContext):
    # TODO: maybe use s3 copy_object()?
    if ctx.code_key is not None:
        bucket = s3_buckets.submissions
        ctx.files_to_clean.add((bucket, ctx.code_key))
        await upload(bucket, ctx.code_key, ctx.code)


async def run_compile_task (ctx: ExecutionContext) -> Optional[CompileResult]:
    if ctx.compile is None:
        return None
    return await run_task(ctx.compile)


def update_dependent_on (record: JudgeTaskRecord, task: JudgeTask):
    # FIXME: make the following code comprehensible
    for testpoint in task.testpoints:
        if testpoint.dependent_on is not None:
            for tp1 in record.task.testpoints:
                if testpoint.dependent_on == tp1.id:
                    status = list(filter(lambda x: x.testpoint_id == tp1.id,
                        record.result.testpoints))
                    if len(status) > 0 and status[0].result == 'accepted':
                        testpoint.dependent_on = None
                    else:
                        testpoint.dependent_on = DependencyNotSatisfied()
                    break

async def run_judge_tasks (ctx: ExecutionContext):
    records = ctx.judge
    ready = list(filter(ctx.dependencies_satisfied, records))
    tasks_running: List[Task[Tuple[JudgeTaskRecord, JudgeResult]]] = []
    def run (task: JudgeTaskRecord):
        async def run_with_rec ():
            return (task, await run_task(task.task))
        return create_task(run_with_rec())
    while len(ready) > 0 or len(tasks_running) > 0:
        tasks_running.extend(map(run, ready))
        done, pending = await wait(tasks_running, return_when=FIRST_COMPLETED)
        tasks_running = list(pending)
        dependents = set()
        for task in done:
            record, res = await task
            record.result = res
            dependents_task = record.plan.dependents
            dependents.update(dependents_task)
            for dependent in dependents_task:
                update_dependent_on(record, records[dependent].plan.task)
        dependents = (records[id] for id in dependents)
        ready = list(filter(ctx.dependencies_satisfied, dependents))


def synthesize_results (results: Iterable[ResultType]) -> ResultType:
    for res in results:
        if res != 'accepted':
            return res
    return 'accepted'

def synthesize_rusage (rusages: Iterable[ResourceUsage]) -> ResourceUsage:
    rusages = list(filter(lambda x: x is not None, rusages))
    return ResourceUsage(
        time_msecs=sum((x.time_msecs for x in rusages), 0),
        memory_bytes=max((x.memory_bytes for x in rusages), default=-1),
        file_count=max((x.file_count for x in rusages), default=-1),
        file_size_bytes=max((x.file_size_bytes for x in rusages), default=-1),
    )

def synthesize_scores (ctx: ExecutionContext) -> ProblemJudgeResult:
    def get_testpoint_results (rec: JudgeTaskRecord):
        return ((x.testpoint_id, x) for x in rec.result.testpoints)
    testpoints: Dict[str, TestpointJudgeResult] = dict(testpoint for list_ in \
        map(get_testpoint_results, ctx.judge) for testpoint in list_)
    rusage = synthesize_rusage(x.resource_usage for x in \
        filter(lambda x: x is not None, testpoints.values()))
    def skipped (name):
        return TestpointJudgeResult(name, 'skipped', 'Skipped')
    def get_group_result (group: TestpointGroup):
        res = list(testpoints[x] if x in testpoints else skipped(x) for x in \
            group.testpoints)
        result = synthesize_results(x.result for x in res)
        score = min(x.score for x in res) * group.score
        return GroupJudgeResult(group.name, result, res, score)
    groups = list(map(get_group_result, ctx.plan.score))
    score = sum(x.score for x in groups)
    result = synthesize_results(x.result for x in groups)
    return ProblemJudgeResult(result, None, score, rusage, groups)


async def execute_plan (plan: JudgePlan, id: int, lang: CodeLanguage,
    code: PosixPath) -> ProblemJudgeResult:
    ctx = ExecutionContext(plan, id, lang, code)
    try:
        ctx.compile = prepare_compile_task(ctx)
        ctx.judge = get_judge_tasks(ctx)
        # print(ctx)
        await upload_code(ctx)
        compile_res = await run_compile_task(ctx)
        if compile_res.result != 'compiled':
            status = 'system_error' if compile_res.result == 'system_error' \
                else 'compile_error'
            return ProblemJudgeResult(status, compile_res.message)
        await run_judge_tasks(ctx)
        return synthesize_scores(ctx)
    finally:
        for bucket, key in ctx.files_to_clean:
            try:
                await remove_file(bucket, key)
            except BaseException as e:
                logger.info(f'Error clearing object: {e}')
