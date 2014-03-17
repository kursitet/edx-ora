# -*- coding: utf-8 -*-
"""
Implements the staff grading views called by the LMS.

General idea: LMS asks for a submission to grade for a course.  Course staff member grades it, submits it back.

Authentication of users must be done by the LMS--this service requires a
login from the LMS to prevent arbitrary clients from connecting, but does not
validate that the passed-in grader_ids correspond to course staff.
"""

import json
import logging
from statsd import statsd

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, Http404
from django.views.decorators.csrf import csrf_exempt

from controller.models import Submission, GraderStatus, SubmissionState
from controller import util
from controller import grader_util
from controller import rubric_functions
import staff_grading_util
from ml_grading import ml_grading_util

from django.db import connection

log = logging.getLogger(__name__)

_INTERFACE_VERSION = 1


@csrf_exempt
@statsd.timed('open_ended_assessment.grading_controller.staff_grading.views.time',
              tags=['function:get_next_submission'])
@util.error_if_not_logged_in
@util.is_submitter
def get_next_submission(request):
    """
    Supports GET request with the following arguments:
    course_id -- the course for which to return a submission.
    grader_id -- LMS user_id of the requesting user

    Returns json dict with the following keys:

    version: '1'  (number)

    success: bool

    if success:
      'submission_id': a unique identifier for the submission, to be passed
                       back with the grade.

      'submission': the submission, rendered as read-only html for grading

      'rubric': the rubric, also rendered as html.

      'prompt': the question prompt, also rendered as html.

      'message': if there was no submission available, but nothing went wrong,
                there will be a message field.
    else:
      'error': if success is False, will have an error message with more info.
    }
    """

    if request.method != "GET":
        raise Http404

    course_id = request.GET.get('course_id')
    grader_id = request.GET.get('grader_id')
    location = request.GET.get('location')
    found = False

    if not (course_id or location) or not grader_id:
        return util._error_response("required_parameter_missing", _INTERFACE_VERSION)

    if location:
        sl = staff_grading_util.StaffLocation(location)
        (found, sid) = sl.next_item()

    # TODO: save the grader id and match it in save_grade to make sure things
    # are consistent.
    if not location:
        sc = staff_grading_util.StaffCourse(course_id)
        (found, sid) = sc.next_item()

    if not found:
        return util._success_response({'message': 'Нет задании данного типа для оценивания.'},
                                      _INTERFACE_VERSION)
    try:
        submission = Submission.objects.get(id=int(sid))
    except Submission.DoesNotExist:
        log.error("Couldn't find submission %s for instructor grading", sid)
        return util._error_response('failed_to_load_submission',
                                    _INTERFACE_VERSION,
                                    data={'submission_id': sid})

    if len(submission.student_response) <= 0:
        student_response = u"Служебная информация: сдан пустой ответ"
    else:
        student_response = submission.student_response

    #Get error metrics from ml grading, and get into dictionary form to pass down to staff grading view
    success, ml_error_info=ml_grading_util.get_ml_errors(submission.location)
    if success:
        ml_error_message=staff_grading_util.generate_ml_error_message(ml_error_info)
    else:
        ml_error_message=ml_error_info

    ml_error_message="Machine learning error information: " + ml_error_message

    sl = staff_grading_util.StaffLocation(submission.location)
    if submission.state != SubmissionState.being_graded:
        log.error("Instructor grading got submission {0} in an invalid state {1} ".format(sid, submission.state))
        return util._error_response('wrong_internal_state',
                                    _INTERFACE_VERSION,
                                    data={'submission_id': sid,
                                     'submission_state': submission.state})

    response = {'submission_id': sid,
                'submission': student_response,
                'rubric': submission.rubric,
                'prompt': submission.prompt,
                'max_score': submission.max_score,
                'ml_error_info' : ml_error_message,
                'problem_name' : submission.problem_id,
                'num_graded' : sl.graded_count(),
                'num_pending' : sl.pending_count(),
                'min_for_ml' : settings.MIN_TO_USE_ML,
                }

    util.log_connection_data()
    return util._success_response(response, _INTERFACE_VERSION)


@csrf_exempt
@statsd.timed(
    'open_ended_assessment.grading_controller.staff_grading.views.time',
    tags=['function:save_grade'])
@util.error_if_not_logged_in
@util.is_submitter
def save_grade(request):
    """
    Supports POST requests with the following arguments:

    course_id: int
    grader_id: int
    submission_id: int
    score: int
    feedback: string

    Returns json dict with keys

    version: int
    success: bool
    error: string, present if not success
    """
    if request.method != "POST":
        return util._error_response("Request needs to be GET", _INTERFACE_VERSION)

    course_id = request.POST.get('course_id')
    grader_id = request.POST.get('grader_id')
    submission_id = request.POST.get('submission_id')
    score = request.POST.get('score')
    feedback = request.POST.get('feedback')
    skipped = request.POST.get('skipped')=="True"
    rubric_scores_complete = request.POST.get('rubric_scores_complete', False)
    rubric_scores = request.POST.getlist('rubric_scores', [])
    is_submission_flagged = request.POST.get('submission_flagged', False)
    if isinstance(is_submission_flagged, basestring):
        is_submission_flagged = is_submission_flagged.lower() == 'true'

    if (# These have to be truthy
        not (course_id and grader_id and submission_id) or
        # These have to be non-None
        score is None or feedback is None):
        return util._error_response("required_parameter_missing", _INTERFACE_VERSION)

    if skipped:
        success, sub=staff_grading_util.set_instructor_grading_item_skipped(submission_id)

        if not success:
            return util._error_response(sub, _INTERFACE_VERSION)

        return util._success_response({}, _INTERFACE_VERSION)

    try:
        score = int(score)
    except ValueError:
        return util._error_response(
            "grade_save_error",
            _INTERFACE_VERSION,
            data={"msg": "Expected integer score.  Got {0}".format(score)})

    try:
        sub=Submission.objects.get(id=submission_id)
    except Exception:
        return util._error_response(
            "grade_save_error",
            _INTERFACE_VERSION,
            data={"msg": "Submission id {0} is not valid.".format(submission_id)}
        )

    first_sub_for_location=Submission.objects.filter(location=sub.location).order_by('date_created')[0]
    rubric= first_sub_for_location.rubric
    rubric_success, parsed_rubric =  rubric_functions.parse_rubric(rubric)

    if rubric_success:
        success, error_message = grader_util.validate_rubric_scores(rubric_scores, rubric_scores_complete, sub)
        if not success:
            return util._error_response(
                "grade_save_error",
                _INTERFACE_VERSION,
                data={"msg": error_message}
            )

    d = {'submission_id': submission_id,
         'score': score,
         'feedback': feedback,
         'grader_id': grader_id,
         'grader_type': 'IN',
         # Humans always succeed (if they grade at all)...
         'status': GraderStatus.success,
         # ...and they're always confident too.
         'confidence': 1.0,
         #And they don't make errors
         'errors' : "",
         'rubric_scores_complete' : rubric_scores_complete,
         'rubric_scores' : rubric_scores,
         'is_submission_flagged' : is_submission_flagged,
         }

    success, header = grader_util.create_and_handle_grader_object(d)

    if not success:
        return util._error_response("grade_save_error", _INTERFACE_VERSION,
                                    data={'msg': 'Internal error'})

    util.log_connection_data()
    return util._success_response({}, _INTERFACE_VERSION)

@csrf_exempt
@statsd.timed('open_ended_assessment.grading_controller.staff_grading.views.time',
    tags=['function:get_problem_list'])
@util.error_if_not_logged_in
@util.is_submitter
def get_problem_list(request):
    """
    Get the list of problems that need grading in course request.GET['course_id'].

    Returns:
        list of dicts with keys
           'location'
           'problem_name'
           'num_graded' -- number graded
           'num_pending' -- number pending in the queue
           'min_for_ml' -- minimum needed to make ML model
    """

    if request.method!="GET":
        error_message="Request needs to be GET."
        log.error(error_message)
        return util._error_response(error_message, _INTERFACE_VERSION)

    course_id=request.GET.get("course_id")

    if not course_id:
        error_message="Missing needed tag course_id"
        log.error(error_message)
        return util._error_response(error_message, _INTERFACE_VERSION)

    locations_for_course = [x['location'] for x in
                            list(Submission.objects.filter(course_id=course_id).values('location').distinct())]

    if len(locations_for_course)==0:
        error_message="No problems associated with course."
        log.error(error_message)
        return util._error_response(error_message, _INTERFACE_VERSION)

    location_info=[]
    for location in locations_for_course:
        sl = staff_grading_util.StaffLocation(location)
        problem_name = sl.problem_name()
        submissions_pending = sl.pending_count()
        finished_instructor_graded = sl.graded_count()
        min_scored_for_location=settings.MIN_TO_USE_PEER
        location_ml_count = Submission.objects.filter(location=location, preferred_grader_type="ML").count()
        if location_ml_count>0:
            min_scored_for_location=settings.MIN_TO_USE_ML

        submissions_required = max([0,min_scored_for_location-finished_instructor_graded])

        problem_name_from_location=location.split("://")[1]
        location_dict={
            'location' : location,
            'problem_name' : problem_name,
            'problem_name_from_location' : problem_name_from_location,
            'num_graded' : finished_instructor_graded,
            'num_pending' : submissions_pending,
            'num_required' : submissions_required,
            'min_for_ml' : settings.MIN_TO_USE_ML,
            }
        location_info.append(location_dict)

    util.log_connection_data()
    return util._success_response({'problem_list' : location_info},
                                  _INTERFACE_VERSION)

@csrf_exempt
@util.error_if_not_logged_in
@util.is_submitter
def get_notifications(request):
    if request.method!="GET":
        error_message="Request needs to be GET."
        log.error(error_message)
        return util._error_response(error_message, _INTERFACE_VERSION)

    course_id=request.GET.get("course_id")

    if not course_id:
        error_message="Missing needed tag course_id"
        log.error(error_message)
        return util._error_response(error_message, _INTERFACE_VERSION)

    sc = staff_grading_util.StaffCourse(course_id)
    success, staff_needs_to_grade = sc.notifications()
    if not success:
        return util._error_response(staff_needs_to_grade, _INTERFACE_VERSION)

    util.log_connection_data()
    return util._success_response({'staff_needs_to_grade' : staff_needs_to_grade}, _INTERFACE_VERSION)
