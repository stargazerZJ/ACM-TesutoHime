import sys
from datetime import datetime
from http.client import BAD_REQUEST

from flask import abort

from commons.models import ContestProblem, JudgeRecord2, Problem
from web.const import Privilege
from web.session_manager import SessionManager
from web.utils import SqlSession, unix_nano
from sqlalchemy import update, select, delete, func

FAR_FUTURE_UNIX_NANO = 253402216962


class ProblemManager:
    @staticmethod
    def add_problem() -> int:
        problem_id = ProblemManager.get_max_id() + 1
        problem = Problem(
            id=problem_id,
            release_time=FAR_FUTURE_UNIX_NANO
        )
        with SqlSession.begin() as db:
            db.add(problem)
        return problem_id

    @staticmethod
    def modify_problem_description(problem_id: int, description: str, problem_input: str, problem_output: str,
                                   example_input: str, example_output: str, data_range: str):
        stmt = update(Problem).where(Problem.id == problem_id) \
            .values(description=description,
                    input=problem_input,
                    output=problem_output,
                    example_input=example_input,
                    example_output=example_output,
                    data_range=data_range)
        try:
            with SqlSession.begin() as db:
                db.execute(stmt)
        except:
            sys.stderr.write("SQL Error in ProblemManager: Modify_Problem\n")

    @staticmethod
    def modify_problem(problem_id: int, title: str, release_time: int, problem_type: int):
        stmt = update(Problem).where(Problem.id == problem_id) \
            .values(title=title,
                    release_time=release_time,
                    problem_type=problem_type)
        try:
            with SqlSession.begin() as db:
                db.execute(stmt)
        except:
            sys.stderr.write("SQL Error in ProblemManager: Modify_Problem\n")

    @staticmethod
    def hide_problem(problem_id: int):
        stmt = update(Problem).where(Problem.id == problem_id) \
            .values(release_time=FAR_FUTURE_UNIX_NANO)
        with SqlSession.begin() as db:
            db.execute(stmt)

    @staticmethod
    def show_problem(problem_id: int):
        now = datetime.now().timestamp()
        stmt = update(Problem).where(Problem.id == problem_id) \
            .values(release_time=now)
        with SqlSession.begin() as db:
            db.execute(stmt)

    @staticmethod
    def get_problem(problem_id: int) -> dict:
        stmt = select(Problem).where(Problem.id == problem_id)
        with SqlSession() as db:
            return db.scalar(stmt)

    @staticmethod
    def modify_problem_limit(problem_id: int, limit: str):
        stmt = update(Problem).where(
            Problem.id == problem_id).values(limits=limit)
        try:
            with SqlSession.begin() as db:
                db.execute(stmt)
        except:
            sys.stderr.write(
                "SQL Error in ProblemManager: Modify_Problem_Limit\n")

    @staticmethod
    def get_title(problem_id: int) -> str:
        stmt = select(Problem.title).where(Problem.id == problem_id)
        with SqlSession() as db:
            data = db.scalar(stmt)
            return data if data is not None else ""

    @staticmethod
    def get_problem_type(problem_id: int) -> int:
        stmt = select(Problem.problem_type).where(Problem.id == problem_id)
        with SqlSession() as db:
            data = db.scalar(stmt)
            return data if data is not None else 0

    @staticmethod
    def get_max_id() -> int:
        stmt = select(func.max(Problem.id)).where(Problem.id < 11000)
        with SqlSession() as db:
            data = db.scalar(stmt)
            return data if data is not None else 0

    @staticmethod
    def should_show(problem: Problem) -> bool:
        return problem is not None and \
            (problem.release_time <= unix_nano()
             or SessionManager.get_privilege() >= Privilege.ADMIN)

    @staticmethod
    def delete_problem(problem_id: int):
        with SqlSession.begin() as db:
            submission_count = db.scalar(select(func.count())
                                         .where(JudgeRecord2.problem_id == problem_id))
            contest_count = db.scalar(select(func.count())
                                      .where(ContestProblem.problem_id == problem_id))
            if submission_count > 0 or contest_count > 0:
                abort(BAD_REQUEST)
            db.execute(delete(Problem).where(Problem.id == problem_id))
