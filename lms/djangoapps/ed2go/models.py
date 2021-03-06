import json
import logging
from datetime import timedelta
from dateutil import parser

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.db import models, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.timezone import now
from jsonfield import JSONField
from opaque_keys.edx.keys import CourseKey
from opaque_keys.edx.locator import BlockUsageLocator
from waffle import switch_is_active

from lms.djangoapps.courseware.courses import get_course
from lms.djangoapps.grades.models import PersistentCourseGrade
from lms.djangoapps.grades.new.course_grade_factory import CourseGradeFactory
from openedx.core.djangoapps.content.course_structures.models import CourseStructure
from openedx.core.djangoapps.xmodule_django.models import CourseKeyField
from student.models import CourseEnrollment, UserProfile

from ed2go import constants as c
from ed2go.exceptions import CompletionProfileAlreadyExists
from ed2go.utils import format_timedelta, generate_username, get_registration_data
from ed2go.xml_handler import XMLHandler

LOG = logging.getLogger(__name__)


def update_enrollment(user, course_key, is_active=True):
    """
    Update a course enrollment object.
    Used to escape sending the save signal.
    """
    CourseEnrollment.objects.filter(
        user=user, course_id=course_key
    ).update(is_active=is_active)


class CourseSession(models.Model):
    """
    Keeps track of how much time a user has spent in a course.
    """
    user = models.ForeignKey(User)
    course_key = CourseKeyField(max_length=255, db_index=True)
    created_at = models.DateTimeField()
    closed_at = models.DateTimeField(blank=True, null=True)
    last_activity_at = models.DateTimeField()
    active = models.BooleanField(default=True, db_index=True)

    class Meta:  # pylint: disable=old-style-class,no-init
        get_latest_by = 'created_at'

    def _update_completion_profile(self):
        """Update the corresponding CompletionProfile to indicate that the session was updated."""
        profile = CompletionProfile.objects.get(user=self.user, course_key=self.course_key)
        profile.to_report = True
        profile.save()

    def save(self, *args, **kwargs):
        if self.pk is None:
            # Only one session per user per course should be active at the same time.
            cls = self.__class__
            qs = cls.objects.filter(user=self.user, course_key=self.course_key, active=True)  # pylint: disable=invalid-name
            for obj in qs:
                obj.close()

            self.created_at = now()
            self.last_activity_at = now()
            self._update_completion_profile()

        return super(CourseSession, self).save(*args, **kwargs)

    def update(self):
        """Updates the last activity time to now. Ignores if the session is not active."""
        if self.active:
            self.last_activity_at = now()
            self.save()
            self._update_completion_profile()

    def close(self, offset_delta=None):
        """
        Closes the current session and sets the closed_at time to now. Ignores if the session is not active.

        Args:
            offset_delta (datetime.timedelta): Time value which is subtracted from closed_at time.
        """
        if self.active:
            self.closed_at = (now() - offset_delta) if offset_delta else now()
            self.active = False
            self.save()
            LOG.info('Session closed for user %s in course %s', self.user, self.course_key)

            self._update_completion_profile()

    @property
    def duration(self):
        """
        Duration of this session.
        If the session is still active, duration is measured from the creation of this
        session to the last activity, if not it's measured from the creation to the closing time.
        """
        last_activity = self.last_activity_at if self.active else self.closed_at
        return last_activity - self.created_at

    @classmethod
    def total_time(cls, user, course_key):
        """
        Total time the user spent in the course, from the first activity to the last.

        Args:
            cls (class): CourseSession class.
            user (User): user whose total session time is retrieved.
            course_key (CourseKey): key of the course where user's activity was measured.

        Returns:
            A timedelta object which represents the total time a user spent in a course.
        """
        qs = cls.objects.filter(user=user, course_key=course_key)  # pylint: disable=invalid-name
        total_duration = timedelta()
        for session in qs:
            total_duration += session.duration
        return total_duration


class CompletionProfile(models.Model):
    """
    Model that keeps track of the user's completion/progress in a given course.
    Metrics are defined by the Ed2go requirements, which are to track whenever
    a user has attempted to solve a problem and watched a video in a course.

    The progress is measured by the formula:
        0.5 * (#_of_attempted_problems / total_problems) + 0.5 * (#_of_watched_videos / total_videos)

    Additional metrics can be seen in the report() method of this model. For now
    they are extracted from existing models in Open edX and don't need
    to be included in this model.
    """
    user = models.ForeignKey(User)
    course_key = CourseKeyField(max_length=255, db_index=True)

    # Indicates whether we need to send a report update for this completion profile.
    # Should be set to True whenever course progress or session is updated.
    to_report = models.BooleanField(default=False)

    registration_key = models.CharField(max_length=255, db_index=True, blank=True)
    reference_id = models.IntegerField(blank=True, null=True)
    active = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        """
        Override of the default save() method.
        Creating an instance of this model enrolls the user into the course.
        """
        if self.pk is None:
            super(CompletionProfile, self).save(*args, **kwargs)
            CourseEnrollment.enroll(self.user, self.course_key)
        else:
            super(CompletionProfile, self).save(*args, **kwargs)

    def update_reference_id(self):
        """Fetch and update the reference ID for this registration."""
        registration_data = get_registration_data(self.registration_key)
        self.reference_id = registration_data[c.REG_REFERENCE_ID]
        self.save()

    def deactivate(self):
        """Unenroll the user prior to deactivating this instance."""
        update_enrollment(self.user, self.course_key, False)
        self.active = False
        self.update_reference_id()
        LOG.info('Deactivated course registration for user %s in course %s.', self.user, self.course_key)

    def activate(self):
        """Enroll the user and activate this instance."""
        update_enrollment(self.user, self.course_key, True)
        self.active = True
        self.update_reference_id()
        LOG.info('Activated course registration for user %s in course %s.', self.user, self.course_key)

    def send_report(self):
        """Sends the generated completion report to the Ed2go completion report endpoint."""
        if switch_is_active(c.ENABLED_ED2GO_COMPLETION_REPORTING):
            report = self.report
            report[c.REQ_API_KEY] = settings.ED2GO_API_KEY
            url = settings.ED2GO_REGISTRATION_SERVICE_URL
            xmlh = XMLHandler()

            data = xmlh.request_data_from_dict({c.REQ_UPDATE_COMPLETION_REPORT: report})
            response = requests.post(url, data=data, headers=xmlh.headers)

            error_msg = 'Error sending completion update report: %s'
            if response.status_code != 200:
                LOG.error(error_msg, response.reason)
                return False

            response_data = xmlh.get_response_data_from_xml(
                response_name=c.RESP_UPDATE_COMPLETION_REPORT,
                xml=response.content
            )
            if response_data[c.RESP_SUCCESS] == 'false':
                LOG.error(error_msg, str(response_data[c.RESP_CODE]))
            else:
                self.to_report = False
                self.save()
                LOG.info('Sent report for completion profile ID %d', self.id)
            return True

    @property
    def report(self):
        """
        Generates a report for the Ed2go completion report endpoint.

        Returns:
            A dictionary containing the report values.
        """
        course_grade = CourseGradeFactory().create(
            self.user,
            get_course(self.course_key)
        )
        persistent_grade = PersistentCourseGrade.objects.filter(
            user_id=self.user.id,
            course_id=self.course_key
        ).first()

        return {
            c.REP_REGISTRATION_KEY: self.registration_key,
            c.REP_PERCENT_PROGRESS: round(self.progress * 100, 2),
            c.REP_LAST_ACCESS_DT: self.user.last_login.strftime('%Y-%m-%dT%H:%M:%SZ'),
            c.REP_COURSE_PASSED: str(course_grade.passed).lower(),
            c.REP_PERCENT_OVERALL_SCORE: course_grade.percent,
            c.REP_COMPLETION_DT: persistent_grade.passed_timestamp if persistent_grade else '',
            c.REP_TIME_SPENT: format_timedelta(CourseSession.total_time(user=self.user, course_key=self.course_key)),
        }

    @property
    def progress(self):
        """
        Returns the user's current progress in a course according to rules:
            * if there are no videos or problems the progress is defaulted to 0.0
            * if there are no videos the progress is measured with
              #_of_attempted_problems / total_problems
            * if there are no problems the progress is measured with
              #_of_watched_videos / total_videos
            * if there are videos and problems in the course, the progress is measured as
              0.5 * (#_of_attempted_problems / total_problems) + 0.5 * (#_of_watched_videos / total_videos)

        Returns:
            A float number that represents the percentage of user's progress in the associated course.
        """
        problems = []
        videos = []
        for chapter in self.chapterprogress.all():
            problems.extend(chapter.units(unit_type=ChapterProgress.UNIT_PROBLEM_TYPE))
            videos.extend(chapter.units(unit_type=ChapterProgress.UNIT_VIDEO_TYPE))

        problems_completed = sum((1.0 if p['done'] else 0.0 for p in problems))
        videos_completed = sum((1.0 if p['done'] else 0.0 for p in videos))

        problems_precent = problems_completed / len(problems) if problems else 1.0
        videos_precent = videos_completed / len(videos) if videos else 1.0

        if not problems and not videos:
            return 0.0
        elif not problems:
            return videos_precent
        elif not videos:
            return problems_precent
        return 0.5 * problems_precent + 0.5 * videos_precent

    @classmethod
    def _create(cls, registration_data):
        """
        Fetches or creates a new user and completion profile
        based on passed in registration data.

        Args:
            registration_data (dict): The registration data fetched from the GetRegistration
                                      API endpoint in dictionary format.

        Returns:
            The new CompletionProfile instance
        """
        student_data = registration_data[c.REG_STUDENT]
        reference_id = registration_data[c.REG_REFERENCE_ID]
        registration_key = registration_data[c.REG_REGISTRATION_KEY]

        course_key_string = c.COURSE_KEY_TEMPLATE.format(
            code=registration_data[c.REG_COURSE][c.REG_CODE]
        )
        course_key = CourseKey.from_string(course_key_string)

        try:
            user = User.objects.get(email=student_data[c.REG_EMAIL])
            completion_profile = CompletionProfile.objects.create(
                user=user,
                registration_key=registration_key,
                reference_id=reference_id,
                course_key=course_key
            )
            LOG.info(
                'Created new Completion Profile [%s] for existing user %s',
                completion_profile.registration_key,
                user.username
            )
        except User.DoesNotExist:
            user = User.objects.create(
                first_name=student_data[c.REG_FIRST_NAME],
                last_name=student_data[c.REG_LAST_NAME],
                username=generate_username(student_data[c.REG_FIRST_NAME]),
                email=student_data[c.REG_EMAIL],
                is_active=True
            )
            year_of_birth = None
            if student_data[c.REG_BIRTHDATE]:
                year_of_birth = parser.parse(student_data[c.REG_BIRTHDATE]).year
            UserProfile.objects.create(
                user=user,
                name=student_data[c.REG_FIRST_NAME] + ' ' + student_data[c.REG_LAST_NAME],
                country=student_data[c.REG_COUNTRY],
                year_of_birth=year_of_birth,
                meta=json.dumps({
                    'ReturnURL': registration_data[c.REG_RETURN_URL],
                    'StudentKey': registration_data[c.REG_STUDENT][c.REG_STUDENT_KEY]
                })
            )
            completion_profile = CompletionProfile.objects.create(
                user=user,
                registration_key=registration_key,
                reference_id=reference_id,
                course_key=course_key
            )
            LOG.info(
                'Created new user [%s] and new Completion Profile [%s]',
                user.username,
                completion_profile.registration_key
            )
        return completion_profile

    @classmethod
    def create_from_key(cls, registration_key):
        """
        Wrapper for the _create() method. Makes a request to the
        Ed2go GetRegistration endpoint using the provided registration
        key and fetches or creates a new user and completion profile.

        Args:
            registration_key (str): The registration key

        Returns:
            The new CompletionProfile instance

        Raises:
            CompletionProfileAlreadyExists: If an attempt was made to create an already
            existing CompletionProfile instance
        """
        if cls.objects.filter(registration_key=registration_key).exists():
            raise CompletionProfileAlreadyExists

        registration_data = get_registration_data(registration_key)
        return CompletionProfile._create(registration_data)

    @classmethod
    def create_from_data(cls, registration_data):
        """
        Wrapper for the _create() method. Uses the passed in registration data
        and fetches or creates a new user and completion profile.

        Args:
            registration_data (dict): The registration data fetched from the GetRegistration
                                      API endpoint in dictionary format.

        Returns:
            The new CompletionProfile instance

        Raises:
            CompletionProfileAlreadyExists: If an attempt was made to create an already
            existing CompletionProfile instance
        """
        registration_key = registration_data[c.REG_REGISTRATION_KEY]
        if cls.objects.filter(registration_key=registration_key).exists():
            raise CompletionProfileAlreadyExists
        return CompletionProfile._create(registration_data)

    @classmethod
    def mark_progress(cls, user, course_key, block_id):
        """
        Marks a block as completed/attempted.

        Args:
            block_type (str): type of the block.
            block_id (str): block_id of the block.
        """
        profile = cls.objects.get(user=user, course_key=course_key)
        for chapter in profile.chapterprogress.all():
            unit = chapter.get_unit(block_id)
            if unit:
                unit['done'] = True
                chapter.save()
                profile.to_report = True
                profile.save()
                LOG.info(
                    'User [%s] progressed on unit [%s]',
                    user.username,
                    block_id
                )
                return True
        return False


@receiver(post_save, sender=CompletionProfile, dispatch_uid='populate_chapter_progress')
@transaction.atomic
def populate_chapter_progress(sender, instance, created, *args, **kwargs):
    """
    After a new Completion Profile instance has been created, this will create
    Chapter Progress instances for all the chapters within the course structure.
    """
    if created:
        course_structure = CourseStructure.objects.get(course_id=instance.course_key).ordered_blocks
        for chapter in course_structure.items()[0][1]['children']:
            ChapterProgress.objects.create(
                chapter_id=chapter,
                completion_profile=instance
            )


class ChapterProgress(models.Model):
    """
    Keeps information about a chapter within a course
    and the progress of a user in that particular chapter.
    """
    UNIT_VIDEO_TYPE = 'video'
    UNIT_PROBLEM_TYPE = 'problem'
    UNIT_TYPES = [UNIT_VIDEO_TYPE, UNIT_PROBLEM_TYPE]

    completion_profile = models.ForeignKey(CompletionProfile, related_name='chapterprogress')
    chapter_id = models.CharField(max_length=255, db_index=True)
    subsections = JSONField(default=dict)

    def _subsection_done(self, subsection_id):
        """
        A subsection is considered done if all the units in it are done
        or when the subsection is viewed in case if it does not have any tracked units.
        """
        subsection = self.subsections[subsection_id]
        if not subsection['units']:
            return subsection['viewed']
        return all((unit['done'] for unit in subsection['units'].values()))

    def units(self, unit_type):
        """
        Returns all the tracked units in this chapter of given unit type.
        """
        units = []
        for sub in self.subsections.values():
            for unit in sub['units'].values():
                if unit['type'] == unit_type:
                    units.append(unit)
        return units

    def get_unit(self, unit_id):
        """
        Returns a single unit with the given unit_id.
        Returns None if there isn't a unit with that ID.
        """
        for subsection in self.subsections.values():
            if unit_id in subsection['units']:
                return subsection['units'][unit_id]
        return None

    @property
    def progress(self):
        """
        Progress percent for the user in this chapter.
        Used on the Learning Path page.
        """
        if not self.subsections:
            return 100
        completed = sum([1.0 if self._subsection_done(sub_id) else 0.0 for sub_id in self.subsections])
        total = len(self.subsections)
        return int(round(completed / total * 100))

    @classmethod
    def mark_subsection_viewed(cls, user, course_key, subsection_id):
        """
        Marks a specific subsection as viewed.

        Returns True if the subsection was already viewed or now marked as viewed,
        and False if it's not found.
        """
        for chapter in cls.objects.filter(
                completion_profile__user=user,
                completion_profile__course_key=course_key
        ):
            if subsection_id in chapter.subsections:
                subsection = chapter.subsections[subsection_id]
                if subsection['viewed']:
                    return True
                subsection['viewed'] = True
                chapter.save()
                return True
        return False


@receiver(post_save, sender=ChapterProgress, dispatch_uid='populate_subsections')
def populate_subsections(sender, instance, created, *args, **kwargs):
    """
    Add all the subsections and tracked units to the ChapterProgress instance.
    Structure of subsections field:

        subsection_id:
            viewed
            units:
                unit_id:
                    type
                    done
    """
    if created:
        course_structure = CourseStructure.objects.get(
            course_id=instance.completion_profile.course_key
        ).ordered_blocks
        subsections = {}

        for section in course_structure[instance.chapter_id]['children']:
            for subsection in course_structure[section]['children']:
                subsection_dict = {'units': {}, 'viewed': False}
                subsection_usage_key = BlockUsageLocator.from_string(subsection)
                subsections.update({
                    subsection_usage_key.block_id: subsection_dict
                })
                for unit in course_structure[subsection]['children']:
                    unit_type = course_structure[unit]['block_type']
                    if unit_type in ChapterProgress.UNIT_TYPES:
                        usage_key = BlockUsageLocator.from_string(unit)
                        subsection_dict['units'].update({
                            usage_key.block_id: {
                                'type': unit_type,
                                'done': False
                            }
                        })
        instance.subsections = subsections
        instance.save()
