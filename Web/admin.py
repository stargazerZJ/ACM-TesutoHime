from http.client import SEE_OTHER
from urllib.parse import urljoin
from uuid import uuid4

import requests
from flask import Blueprint, abort, redirect, render_template, request
from requests.exceptions import RequestException

from config import ProblemConfig, S3Config, SchedulerConfig
from const import *
from contestManager import Contest_Manager
from judgeManager import Judge_Manager
from judgeServerScheduler import JudgeServer_Scheduler
from problemManager import Problem_Manager
from referenceManager import Reference_Manager
from sessionManager import Login_Manager
from userManager import User_Manager
from utils import (NotFoundException, generate_s3_public_url, mark_void2,
                   rejudge2)

admin = Blueprint('admin', __name__, static_folder='static')


# TODO(Pioooooo): validate data
def _validate_user_data(form):
    if String.TYPE not in form:
        return ReturnCode.ERR_BAD_DATA
    # TODO: validate username
    op = int(form[String.TYPE])
    if 0 <= op < 2:
        # TODO: validate
        return None
    return None


def _validate_problem_data(form):
    if String.TYPE not in form:
        return ReturnCode.ERR_BAD_DATA
    op = int(form[String.TYPE])
    if 1 <= op < 3:
        # TODO: validate ID
        return None
    if 0 <= op < 2:
        # TODO: validate
        return None
    return None


def _validate_contest_data(form):
    # if String.TYPE not in form:
        # return ReturnCode.ERR_BAD_DATA
    op = int(form[String.TYPE])
    if 0 <= op <= 1:
        if int(form[String.START_TIME]) > int(form[String.END_TIME]):
            return ReturnCode.ERR_CONTEST_ENDTIME_BEFORE_START_TIME
    # if 1 <= op < 6:
        # TODO: validate contest ID
        # return None
    # if 0 <= op < 2:
        # TODO: validate
        # return None
    # elif 3 <= op < 5:
        # TODO: validate problem ID
        # return None
    # elif 5 <= op < 6:
        # TODO: validate username
        # return None
    return None


@admin.route('/')
def index():
    privilege = Login_Manager.get_privilege()
    if privilege < Privilege.ADMIN:
        abort(404)
    return render_template('admin.html', privilege=privilege, Privilege=Privilege, is_Admin=True,
                           friendlyName=Login_Manager.get_friendly_name())

@admin.route('/admin_doc')
def admin_doc():
    privilege = Login_Manager.get_privilege()
    if privilege < Privilege.ADMIN:
        abort(404)
    return render_template('admin_doc.html')

@admin.route('/problem_format_doc')
def problem_format_doc():
    privilege = Login_Manager.get_privilege()
    if privilege < Privilege.ADMIN:
        abort(404)
    return render_template('problem_format_doc.html')

@admin.route('/data_doc')
def data_doc():
    privilege = Login_Manager.get_privilege()
    if privilege < Privilege.ADMIN:
        abort(404)
    return render_template('data_doc.html')

@admin.route('/user', methods=['post'])
def user_manager():
    if Login_Manager.get_privilege() < Privilege.SUPER:
        abort(404)
    form = request.json
    # err = _validate_user_data(form)
    # if err is not None:
    #     return err
    try:
        op = int(form[String.TYPE])
        if op == 0:
            User_Manager.add_user(form[String.USERNAME], int(form[String.STUDENT_ID]), form[String.FRIENDLY_NAME],
                                  form[String.PASSWORD], form[String.PRIVILEGE])
            return ReturnCode.SUC_ADD_USER
        elif op == 1:
            User_Manager.modify_user(form[String.USERNAME], form.get(String.STUDENT_ID, None),
                                     form.get(String.FRIENDLY_NAME, None), form.get(String.PASSWORD, None),
                                     form.get(String.PRIVILEGE, None))
            return ReturnCode.SUC_MOD_USER
        elif op == 2:
            User_Manager.delete_user(form[String.USERNAME])
            return ReturnCode.SUC_DEL_USER
        else:
            return ReturnCode.ERR_BAD_DATA
    except KeyError:
        return ReturnCode.ERR_BAD_DATA
    except TypeError:
        return ReturnCode.ERR_BAD_DATA


@admin.route('/problem', methods=['post'])
def problem_manager():
    if Login_Manager.get_privilege() < Privilege.ADMIN:
        abort(404)
    form = request.json
    # err = _validate_problem_data(form)
    # if err is not None:
    #     return err
    try:
        op = int(form[String.TYPE])
        if op == 0:
            Problem_Manager.add_problem(int(form[String.PROBLEM_ID]), form.get(String.TITLE, None),
                                        form.get(String.DESCRIPTION, None), form.get(String.INPUT, None),
                                        form.get(String.OUTPUT, None), form.get(String.EXAMPLE_INPUT, None),
                                        form.get(String.EXAMPLE_OUTPUT, None), form.get(String.DATA_RANGE, None),
                                        form.get(String.RELEASE_TIME, None), form.get(String.PROBLEM_TYPE, None))
            return ReturnCode.SUC_ADD_PROBLEM
        elif op == 1:
            Problem_Manager.modify_problem(int(form[String.PROBLEM_ID]), form.get(String.TITLE, None),
                                           form.get(String.DESCRIPTION, None), form.get(String.INPUT, None),
                                           form.get(String.OUTPUT, None), form.get(String.EXAMPLE_INPUT, None),
                                           form.get(String.EXAMPLE_OUTPUT, None), form.get(String.DATA_RANGE, None),
                                           form.get(String.RELEASE_TIME, None), form.get(String.PROBLEM_TYPE, None))
            return ReturnCode.SUC_MOD_PROBLEM
        elif op == 2:
            Problem_Manager.delete_problem(form[String.PROBLEM_ID])
            return ReturnCode.SUC_DEL_PROBLEM
        else:
            return ReturnCode.ERR_BAD_DATA
    except KeyError:
        return ReturnCode.ERR_BAD_DATA
    except TypeError:
        return ReturnCode.ERR_BAD_DATA

@admin.route('/problem_limit', methods=['post'])
def problem_limit():
    if Login_Manager.get_privilege() < Privilege.ADMIN:
        abort(404)
    form = request.form
    # err = _validate_problem_data(form)
    # if err is not None:
    #     return err
    try:
        Problem_Manager.modify_problem_limit(form["id"], form["data"])
        return ReturnCode.SUC_ADD_PROBLEM
    except KeyError:
        return ReturnCode.ERR_BAD_DATA
    except TypeError:
        return ReturnCode.ERR_BAD_DATA


@admin.route('/contest', methods=['post'])
def contest_manager():
    if Login_Manager.get_privilege() < Privilege.ADMIN:
        abort(404)
    form = request.json
    err = _validate_contest_data(form)
    if err is not None:
        return err
    try:
        op = int(form[String.TYPE])
        if op == 0:
            Contest_Manager.create_contest(int(form[String.CONTEST_ID]), form[String.CONTEST_NAME], int(form[String.START_TIME]),
                                           int(form[String.END_TIME]), int(form[String.CONTEST_TYPE]))
            return ReturnCode.SUC_ADD_CONTEST
        elif op == 1:
            Contest_Manager.modify_contest(int(form[String.CONTEST_ID]), form.get(String.CONTEST_NAME, None),
                                           int(form.get(String.START_TIME, None)), int(form.get(String.END_TIME, None)),
                                           int(form.get(String.CONTEST_TYPE, None)))
            return ReturnCode.SUC_MOD_CONTEST
        elif op == 2:
            Contest_Manager.delete_contest(int(form[String.CONTEST_ID]))
            return ReturnCode.SUC_DEL_CONTEST
        elif op == 3:
            for problemId in form[String.CONTEST_PROBLEM_IDS]:
                Contest_Manager.add_problem_to_contest(int(form[String.CONTEST_ID]), int(problemId))
            return ReturnCode.SUC_ADD_PROBLEMS_TO_CONTEST
        elif op == 4:
            for problemId in form[String.CONTEST_PROBLEM_IDS]:
                Contest_Manager.delete_problem_from_contest(int(form[String.CONTEST_ID]), int(problemId))
            return ReturnCode.SUC_DEL_PROBLEMS_FROM_CONTEST
        elif op == 5:
            for username in form[String.CONTEST_USERNAMES]:
                Contest_Manager.add_player_to_contest(int(form[String.CONTEST_ID]), username)
            return ReturnCode.SUC_ADD_USERS_TO_CONTEST
        elif op == 6:
            for username in form[String.CONTEST_USERNAMES]:
                Contest_Manager.delete_player_from_contest(int(form[String.CONTEST_ID]), username)
            return ReturnCode.SUC_DEL_USERS_FROM_CONTEST
        else:
            return ReturnCode.ERR_BAD_DATA
    except KeyError:
        return ReturnCode.ERR_BAD_DATA
    except TypeError:
        return ReturnCode.ERR_BAD_DATA


@admin.route('/problem/<int:problem_id>/upload-url')
def data_upload(problem_id):
    if Login_Manager.get_privilege() < Privilege.ADMIN:
        abort(404)
    return generate_s3_public_url('put_object', {
        'Bucket': S3Config.Buckets.problems,
        'Key': f'{problem_id}.zip',
    }, ExpiresIn=3600)


@admin.route('/problem/<int:problem_id>/update', methods=['POST'])
def data_update(problem_id):
    url = urljoin(SchedulerConfig.base_url, f'problem/{problem_id}/update')
    res = requests.post(url).json()
    if res['result'] == 'ok':
        return 'ok'
    elif res['result'] == 'invalid problem':
        return f'Invalid problem: {res["error"]}'
    elif res['result'] == 'system error':
        return f'System error: {res["error"]}'
    return 'Bad result from scheduler'


@admin.route('/data_download', methods=['POST'])
def data_download():
    if Login_Manager.get_privilege() < Privilege.ADMIN:
        abort(404)
    id = request.form['id']
    key = f'{id}.zip'
    url = generate_s3_public_url('get_object', {
        'Bucket': S3Config.Buckets.problems,
        'Key': key,
    }, ExpiresIn=3600)
    return redirect(url, SEE_OTHER)


max_pic_size = 10485760

@admin.route('/pic-url', methods=['POST'])
def pic_upload():
    if Login_Manager.get_privilege() < Privilege.ADMIN:
        abort(404)
    length = int(request.form['length'])
    if length > max_pic_size:
        abort(413)
    if length <= 0:
        abort(400)
    type = str(request.form['type'])
    if not type.startswith('image/'):
        abort(400)
    return generate_s3_public_url('put_object', {
        'Bucket': S3Config.Buckets.images,
        'Key': str(uuid4()),
        'ContentLength': length,
        'ContentType': type,
    }, ExpiresIn=3600)

@admin.route('/rejudge', methods=['POST'])
def rejudge():
    if Login_Manager.get_privilege() < Privilege.ADMIN:
        abort(404)

    type = request.form['type']

    if type == "by_judge_id":
        id = request.form['judge_id']
        id_list = id.strip().splitlines()
        try:
            for i in id_list:
                rejudge2(i)
            return ReturnCode.SUC_REJUDGE
        except NotFoundException:
            return ReturnCode.ERR_BAD_DATA
    elif type == "by_problem_id":
        ids = request.form['problem_id'].strip().splitlines()
        try:
            for id in ids:
                record = Judge_Manager.search_judge(None, id, None, None)
                for i in record:
                    rejudge2(i['ID'])
            return ReturnCode.SUC_REJUDGE
        except NotFoundException:
            return ReturnCode.ERR_BAD_DATA

@admin.route('/disable_judge', methods=['POST'])
def disable_judge():
    if Login_Manager.get_privilege() < Privilege.ADMIN:
        abort(404)

    type = request.form['type']

    if type == "by_judge_id":
        id = request.form['judge_id']
        id_list = id.strip().splitlines()
        try:
            for i in id_list:
                mark_void2(i)
            return ReturnCode.SUC_DISABLE_JUDGE
        except NotFoundException:
            return ReturnCode.ERR_BAD_DATA
    elif type == "by_problem_id":
        ids = request.form['problem_id'].strip().splitlines()
        try:
            for id in ids:
                record = Judge_Manager.search_judge(None, id, None, None)
                for i in record:
                    mark_void2(i['ID'])
            return ReturnCode.SUC_DISABLE_JUDGE
        except NotFoundException:
            return ReturnCode.ERR_BAD_DATA

@admin.route('/add_judge', methods=['POST'])
def add_judge():
    if Login_Manager.get_privilege() < Privilege.ADMIN:
        abort(404)
    server_friendly_name = request.form['server']
    problem_id = int(request.form['problem_id'])
    if problem_id < 1000 or (problem_id > Problem_Manager.get_max_id() and problem_id < 11000) or problem_id > Problem_Manager.get_real_max_id():
        return ReturnCode.ERR_ADD_JUDGE_PROBLEM_ID
    username = str(request.form['username'])
    if User_Manager.validate_username(username):
        return ReturnCode.ERR_ADD_JUDGE_USERNAME
    lang = -1
    lang_request_str = str(request.form['lang'])
    if lang_request_str == 'cpp': 
        lang = 0
    elif lang_request_str == 'git':
        lang = 1
    elif lang_request_str == 'Verilog':
        lang = 2
    elif lang_request_str == 'quiz':
        lang = 3
    user_code = request.form['user_code']
    if len(str(user_code)) > ProblemConfig.Max_Code_Length:
        return ReturnCode.ERR_ADD_JUDGE_CODE_LENGTH
    share = request.form['share']
    if(share == '1'):
        share = True
    else:
        share = False
    try:
        if len(server_friendly_name) == 0: # no server specified. normal submit
            JudgeServer_Scheduler.Start_Judge(problem_id, username, user_code, lang, share)
            return ReturnCode.SUC_ADD_JUDGE
        else:
            exit_code = JudgeServer_Scheduler.Start_Targeted_Judge(problem_id, username, user_code, lang, share, server_friendly_name)
            if exit_code == 0:
                return ReturnCode.SUC_ADD_JUDGE
            elif exit_code == -1:
                return ReturnCode.ERR_ADD_JUDGE_SERVER_INVALID
            else:
                return ReturnCode.ERR_ADD_JUDGE_BUSY
    except RequestException:
        return ReturnCode.ERR_BAD_DATA

@admin.route('/add_realname', methods=['POST'])
def add_realname():
    if Login_Manager.get_privilege() < Privilege.ADMIN:
        abort(404)
    student_id = request.form['student_id']
    student_id_list = student_id.strip().splitlines()
    year_course_name = request.form['year_course_name']
    year_course_name_list = year_course_name.strip().splitlines()
    try:
        for i in range(0, len(student_id_list)):
            Reference_Manager.Add_Student(student_id_list[i], year_course_name_list[i])
        return ReturnCode.SUC_ADD_REALNAME
    except RequestException:
        return ReturnCode.ERR_BAD_DATA
