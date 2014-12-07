from __future__ import unicode_literals
from django.db import models
from django.utils import timezone
import datetime
from django.conf import settings
from south.modelsinspector import add_introspection_rules

class GraderStatus():
    failure="F"
    success="S"

class SubmissionState():
    being_graded="C"
    waiting_to_be_graded="W"
    finished="F"
    flagged= "L"
    skipped="S"

class NotificationTypes():
    peer_grading = 'student_needs_to_peer_grade'
    staff_grading = 'staff_needs_to_grade'
    flagged_submissions = 'flagged_submissions_exist'
    new_grading_to_view = 'new_student_grading_to_view'
    overall = 'overall_need_to_check'

GRADER_TYPE = (
    ('ML', 'ML'),
    ('IN', 'Instructor'),
    ('PE', 'Peer'),
    ('SE', 'Self'),
    ('NA', 'None'),
    ('BC', 'Basic Check'),
    )

STATUS_CODES = (
    (GraderStatus.success, "Success"),
    (GraderStatus.failure, "Failure"),
    )

STATE_CODES = (
    (SubmissionState.being_graded, "Currently being Graded"),
    (SubmissionState.waiting_to_be_graded, "Waiting to be Graded"),
    (SubmissionState.finished, "Finished" ),
    (SubmissionState.flagged, "Flagged" )
    )

CHARFIELD_LEN_SMALL = 128
CHARFIELD_LEN_LONG = 1024

# TODO: DB settings -- utf-8, innodb, store everything in UTC

class Submission(models.Model):
    # controller state
    preferred_grader_type = models.CharField(max_length=2, choices=GRADER_TYPE, default="NA")
    next_grader_type = models.CharField(max_length=2, choices=GRADER_TYPE, default="NA")
    previous_grader_type = models.CharField(max_length=2, choices=GRADER_TYPE, default="NA")
    state = models.CharField(max_length=1, choices=STATE_CODES)
    grader_settings = models.TextField(default="")

    # data about the submission
    date_created = models.DateTimeField(auto_now_add=True)
    date_modified = models.DateTimeField(auto_now=True)
    prompt = models.TextField(default="")
    rubric = models.TextField(default="")
    initial_display = models.TextField(default="")
    answer = models.TextField(default="")
    # TODO: is this good enough?  unique per problem/student?
    student_id = models.CharField(max_length=CHARFIELD_LEN_SMALL, db_index = True)

    # specified in the input type--can be reused between many different
    # problems.  (Should perhaps be named something like problem_type)
    problem_id = models.CharField(max_length=CHARFIELD_LEN_LONG)

    # passed by the LMS
    location = models.CharField(max_length=CHARFIELD_LEN_SMALL, default="", db_index = True)
    max_score = models.IntegerField(default=1)
    course_id = models.CharField(max_length=CHARFIELD_LEN_SMALL)
    student_response = models.TextField(default="")
    student_submission_time = models.DateTimeField(default=timezone.now)

    # xqueue details
    xqueue_submission_id = models.CharField(max_length=CHARFIELD_LEN_SMALL, unique=True)
    xqueue_submission_key = models.CharField(max_length=CHARFIELD_LEN_LONG, default="")
    xqueue_queue_name = models.CharField(max_length=CHARFIELD_LEN_SMALL, default="")
    posted_results_back_to_queue = models.BooleanField(default=False)

    #Plagiarism/duplicate checking
    is_duplicate = models.BooleanField(default=False)
    is_plagiarized = models.BooleanField(default=False)
    duplicate_submission_id = models.IntegerField(null=True, blank=True)
    has_been_duplicate_checked = models.BooleanField(default=False)

    #Control logic passed from the LMS
    skip_basic_checks = models.BooleanField(default = False)
    control_fields = models.TextField(default="")

    def __unicode__(self):
        sub_row = "Essay to be graded from student {0}, in course {1}, and problem {2}.  ".format(
            self.student_id, self.course_id, self.problem_id)
        sub_row += "Submission created at {0} and modified at {1}.  ".format(self.date_created, self.date_modified)
        sub_row += "Current state is {0}, next grader is {1},".format(self.state, self.next_grader_type)
        sub_row += " previous grader is {0}".format(self.previous_grader_type)
        return sub_row

    def get_all_graders(self):
        return self.grader_set.all()

    def get_last_grader(self):
        all_graders = self.get_all_graders()
        grader_times = [x.date_created for x in all_graders]
        last_grader = all_graders[grader_times.index(max(grader_times))]
        return last_grader

    def set_previous_grader_type(self):
        last_grader = self.get_last_grader()
        self.previous_grader_type = last_grader.grader_type
        self.save()
        return "Save ok."

    def get_successful_peer_graders(self):
        all_graders = self.get_all_graders()
        successful_peer_graders = all_graders.filter(
            status_code=GraderStatus.success,
            grader_type="PE",
        )
        return successful_peer_graders

    def get_successful_graders(self):
        all_graders = self.get_all_graders()
        successful_graders = all_graders.filter(
            status_code=GraderStatus.success,
        )
        return successful_graders

    def get_unsuccessful_graders(self):
        all_graders = self.get_all_graders()
        unsuccessful_graders = all_graders.filter(
            status_code=GraderStatus.failure,
        )
        return unsuccessful_graders

    def get_all_successful_scores_and_feedback(self):
        rubric_scores_complete = False
        all_graders = list(self.get_successful_graders().order_by("-date_modified"))
        all_graders_types = [g.grader_type for g in all_graders]
        #If no graders succeeded, send back the feedback from the last unsuccessful submission (which should be an error message).
        if len(all_graders) == 0:
            last_grader=self.get_unsuccessful_graders().order_by("-date_modified")[0]
            return_dict = {'score': 0, 'feedback': last_grader.feedback, 'grader_type' : last_grader.grader_type,
                    'success' : False, 'grader_id' : last_grader.id, 'submission_id' : self.id, 'student_id' : self.student_id}
            return_dict.update(last_grader.check_for_and_return_latest_rubric())
            return_dict.update(last_grader.get_latest_rubric_headers_and_scores())
            return return_dict
        #If grader is ML or instructor, only send back last successful submission
        elif (all_graders[0].grader_type in ["IN", "ML"] or 
              all_graders[0].grader_type == "BC" and "PE" not in all_graders_types):
            return_dict =  {'score': all_graders[0].score, 'feedback': all_graders[0].feedback,
                    'grader_type' : all_graders[0].grader_type, 'success' : True,
                    'grader_id' : all_graders[0].id , 'submission_id' : self.id , 'student_id' : self.student_id}
            return_dict.update(all_graders[0].check_for_and_return_latest_rubric())
            return_dict.update(all_graders[0].get_latest_rubric_headers_and_scores())
            return return_dict
        #If grader is peer, send back all peer judgements
        elif (self.previous_grader_type == "PE" or 
              all_graders[0].grader_type == "BC" and "PE" in all_graders_types):
            peer_graders = [p for p in all_graders if p.grader_type == "PE"][:settings.MAX_GRADER_COUNT]
            combined_rubrics = [p.check_for_and_return_latest_rubric() for p in peer_graders]
            rubric_headers = [p.get_latest_rubric_headers_and_scores().get("rubric_headers", []) for p in peer_graders]
            rubric_scores = [p.get_latest_rubric_headers_and_scores().get("rubric_scores", []) for p in peer_graders]
            rubric_xml = [cr['rubric_xml'] for cr in combined_rubrics]
            rubric_scores_complete = [cr['rubric_scores_complete'] for cr in combined_rubrics]
            score = [p.score for p in peer_graders]
            feedback = [p.feedback for p in peer_graders]
            grader_ids=[p.id for p in peer_graders]
            return {'score': score, 'feedback': feedback, 'grader_type' : "PE", 'success' : True,
                    'grader_id' : grader_ids, 'submission_id' : self.id, 'rubric_xml' : rubric_xml,
                    'rubric_scores_complete' : rubric_scores_complete, "rubric_headers" : rubric_headers,
                    "rubric_scores" : rubric_scores, 'student_id' : self.student_id}
        else:
            return {'score': -1, 'feedback' : "There was an error with your submission.",
                    'grader_type' : self.previous_grader_type, 'success' : False, 'rubric_scores_complete' : False,
                    'rubric_xml' : "", "rubric_headers" : [], "rubric_scores" : [], 'student_id' : self.student_id}

    def get_last_successful_instructor_grader(self):
        all_graders = self.get_all_graders()
        successful_instructor_graders = all_graders.filter(
            status_code=GraderStatus.success,
            grader_type="IN",
        ).order_by("-date_created")
        if successful_instructor_graders.count() == 0:
            return {'score': -1, 'rubric' : ""}

        last_successful_instructor = successful_instructor_graders[0]
        return {'score': last_successful_instructor.score, 'rubric' : last_successful_instructor.check_for_and_return_latest_rubric()['rubric_xml'], 'feedback' : last_successful_instructor.feedback}

    def get_oldest_unassociated_timing_object(self):
        all_timing=self.timing_set.filter(
            finished_timing=False,
        ).order_by("-date_modified")[:1]

        if all_timing.count()==0:
            return False, "Could not find timing object"

        return True, all_timing[0]

class Grader(models.Model):
    submission = models.ForeignKey('Submission', db_index = True)
    score = models.IntegerField()
    feedback = models.TextField()
    status_code = models.CharField(max_length=1, choices=STATUS_CODES)
    date_created = models.DateTimeField(auto_now_add=True)
    date_modified = models.DateTimeField(auto_now=True)

    # For human grading, this is the id of the user that graded the submission.
    # For machine grading, it's the name and version of the algorithm that was
    # used.
    grader_id = models.CharField(max_length=CHARFIELD_LEN_LONG, default="1")
    grader_type = models.CharField(max_length=2, choices=GRADER_TYPE)

    # should be between 0 and 1, with 1 being most confident.
    confidence = models.DecimalField(max_digits=10, decimal_places=9, default=0)

    #User for instructor grading to mark essays as calibration or not.
    is_calibration = models.BooleanField(default=False)

    def __unicode__(self):
        sub_row = "Grader object for submission {0} with status code {1}. ".format(self.submission.id, self.status_code)
        sub_row += "Grader type {0}, created on {1}, modified on {2}. ".format(self.grader_type, self.date_created,
            self.date_modified)
        return sub_row

    def has_rubric(self):
        return self.rubric_set.count()>0

    def get_latest_rubric(self):
        latest_rubric=self.rubric_set.filter(finished_scoring=True).order_by('-date_created')[0]
        return latest_rubric

    def check_for_and_return_latest_rubric(self):
        latest_rubric={'rubric_xml': "", 'rubric_scores_complete' : False}
        if self.has_rubric():
            latest_rubric_object=self.get_latest_rubric()
            latest_rubric['rubric_xml']=latest_rubric_object.format_rubric()
            latest_rubric['rubric_scores_complete']=True
        return latest_rubric

    def get_latest_rubric_headers_and_scores(self):
        rubric_headers_and_scores = {"rubric_headers" : [], "rubric_scores" : []}
        if self.has_rubric():
            latest_rubric_object=self.get_latest_rubric()
            headers = latest_rubric_object.get_rubric_headers()
            scores = latest_rubric_object.get_rubric_scores()
            rubric_headers_and_scores['rubric_scores'] = scores
            rubric_headers_and_scores['rubric_headers'] = headers

        return rubric_headers_and_scores

class Message(models.Model):
    grader = models.ForeignKey('Grader', db_index = True)
    message = models.TextField()
    originator = models.CharField(max_length=CHARFIELD_LEN_SMALL)
    recipient= models.CharField(max_length=CHARFIELD_LEN_SMALL)
    message_type= models.CharField(max_length=CHARFIELD_LEN_SMALL)
    score = models.IntegerField(null=True, blank=True)

    date_created = models.DateTimeField(auto_now_add=True)
    date_modified = models.DateTimeField(auto_now=True)

class Rubric(models.Model):
    """
    Each rubric encapsulates how a student was graded according to a particular rubric
    """
    grader = models.ForeignKey('Grader', db_index = True)
    rubric_version = models.CharField(max_length=CHARFIELD_LEN_SMALL)
    finished_scoring = models.BooleanField(default=False)

    date_created = models.DateTimeField(auto_now_add=True)
    date_modified = models.DateTimeField(auto_now=True)

    def format_rubric(self):
        formatted_rubric="<rubric>"
        rubric_items = self.rubricitem_set.all().order_by('item_number')
        for ri in rubric_items:
            formatted_rubric+=ri.format_rubric_item()
        formatted_rubric+="</rubric>"
        return formatted_rubric

    def get_rubric_scores(self):
        rubric_items = self.rubricitem_set.all().order_by('item_number')
        rubric_scores = []
        for ri in rubric_items:
            rubric_scores.append(float(ri.score))
        return rubric_scores

    def get_rubric_headers(self):
        rubric_items = self.rubricitem_set.all().order_by('item_number')
        rubric_headers = []
        for ri in rubric_items:
            rubric_headers.append(ri.text)
        return rubric_headers

class RubricItem(models.Model):
    """
    Each one encapsulates one item in a rubric, along with comments and the score on the item
    """

    rubric=models.ForeignKey('Rubric', db_index = True)
    text = models.TextField()
    short_text=models.CharField(max_length=CHARFIELD_LEN_LONG, default="")
    comment = models.TextField(default="")
    score=models.DecimalField(max_digits=10, decimal_places=2, default=0)
    max_score= models.IntegerField(default=1)
    finished_scoring = models.BooleanField(default=False)

    #Ensures that rubric items are ordered properly
    item_number = models.IntegerField()

    #Everybody likes date/time information!
    date_created = models.DateTimeField(auto_now_add=True)
    date_modified = models.DateTimeField(auto_now=True)

    def format_rubric_item(self):
        formatted_item=""
        formatted_item+="<category>"
        formatted_item+="<description>{0}</description>".format(self.text)
        formatted_item+="<score>{0}</score>".format(int(self.score))
        for option in self.rubricoption_set.all().order_by('item_number'):
            formatted_item+=option.format_rubric_option()
        formatted_item+="</category>"
        return formatted_item

class RubricOption(models.Model):

    rubric_item=models.ForeignKey('RubricItem', db_index = True)
    points = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    short_text = models.CharField(max_length=CHARFIELD_LEN_SMALL, default="")
    text = models.TextField()
    item_number = models.IntegerField()

    def format_rubric_option(self):
        formatted_item="<option points='{0}'>{1}</option>".format(int(self.points), self.text)
        return formatted_item


