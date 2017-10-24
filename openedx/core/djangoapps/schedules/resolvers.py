import datetime
from itertools import groupby
import logging

import attr
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.staticfiles.templatetags.staticfiles import static
from django.core.urlresolvers import reverse
from django.db.models import F, Q
from django.utils.formats import dateformat, get_format


from edx_ace.recipient_resolver import RecipientResolver
from edx_ace.recipient import Recipient

from courseware.date_summary import verified_upgrade_deadline_link, verified_upgrade_link_is_valid
from openedx.core.djangoapps.monitoring_utils import function_trace, set_custom_metric
from openedx.core.djangoapps.schedules.config import COURSE_UPDATE_WAFFLE_FLAG
from openedx.core.djangoapps.schedules.exceptions import CourseUpdateDoesNotExist
from openedx.core.djangoapps.schedules.models import Schedule
from openedx.core.djangoapps.schedules.utils import PrefixedDebugLoggerMixin
from openedx.core.djangoapps.schedules.template_context import (
    absolute_url,
    get_base_template_context
)

from request_cache.middleware import request_cached
from xmodule.modulestore.django import modulestore


LOG = logging.getLogger(__name__)

DEFAULT_NUM_BINS = 24
RECURRING_NUDGE_NUM_BINS = DEFAULT_NUM_BINS
UPGRADE_REMINDER_NUM_BINS = DEFAULT_NUM_BINS
COURSE_UPDATE_NUM_BINS = DEFAULT_NUM_BINS


@attr.s
class BinnedSchedulesBaseResolver(PrefixedDebugLoggerMixin, RecipientResolver):
    """
    Identifies learners to send messages to, pulls all needed context and sends a message to each learner.

    Note that for performance reasons, it actually enqueues a task to send the message instead of sending the message
    directly.

    Arguments:
        async_send_task -- celery task function that sends the message
        site -- Site object that filtered Schedules will be a part of
        target_datetime -- datetime that the User's Schedule's schedule_date_field value should fall under
        day_offset -- int number of days relative to the Schedule's schedule_date_field that we are targeting
        bin_num -- int for selecting the bin of Users whose id % num_bins == bin_num
        org_list -- list of course_org names (strings) that the returned Schedules must or must not be in
                    (default: None)
        exclude_orgs -- boolean indicating whether the returned Schedules should exclude (True) the course_orgs in
                        org_list or strictly include (False) them (default: False)
        override_recipient_email -- string email address that should receive all emails instead of the normal
                                    recipient. (default: None)

    Static attributes:
        schedule_date_field -- the name of the model field that represents the date that offsets should be computed
                               relative to. For example, if this resolver finds schedules that started 7 days ago
                               this variable should be set to "start".
        num_bins -- the int number of bins to split the users into
    """
    async_send_task = attr.ib()
    site = attr.ib()
    target_datetime = attr.ib()
    day_offset = attr.ib()
    bin_num = attr.ib()
    org_list = attr.ib()
    exclude_orgs = attr.ib(default=False)
    override_recipient_email = attr.ib(default=None)

    schedule_date_field = None
    num_bins = DEFAULT_NUM_BINS

    def __attrs_post_init__(self):
        # TODO: in the next refactor of this task, pass in current_datetime instead of reproducing it here
        self.current_datetime = self.target_datetime - datetime.timedelta(days=self.day_offset)

    def send(self, msg_type):
        for (user, language, context) in self.schedules_for_bin():
            msg = msg_type.personalize(
                Recipient(
                    user.username,
                    self.override_recipient_email or user.email,
                ),
                language,
                context,
            )
            with function_trace('enqueue_send_task'):
                self.async_send_task.apply_async((self.site.id, str(msg)), retry=False)

    def get_schedules_with_target_date_by_bin_and_orgs(
        self, order_by='enrollment__user__id'
    ):
        """
        Returns Schedules with the target_date, related to Users whose id matches the bin_num, and filtered by org_list.

        Arguments:
        order_by -- string for field to sort the resulting Schedules by
        """
        target_day = _get_datetime_beginning_of_day(self.target_datetime)
        schedule_day_equals_target_day_filter = {
            'courseenrollment__schedule__{}__gte'.format(self.schedule_date_field): target_day,
            'courseenrollment__schedule__{}__lt'.format(self.schedule_date_field): target_day + datetime.timedelta(days=1),
        }
        users = User.objects.filter(
            courseenrollment__is_active=True,
            **schedule_day_equals_target_day_filter
        ).annotate(
            id_mod=F('id') % self.num_bins
        ).filter(
            id_mod=self.bin_num
        )

        schedule_day_equals_target_day_filter = {
            '{}__gte'.format(self.schedule_date_field): target_day,
            '{}__lt'.format(self.schedule_date_field): target_day + datetime.timedelta(days=1),
        }
        schedules = Schedule.objects.select_related(
            'enrollment__user__profile',
            'enrollment__course',
        ).prefetch_related(
            'enrollment__course__modes'
        ).filter(
            Q(enrollment__course__end__isnull=True) | Q(
                enrollment__course__end__gte=self.current_datetime),
            enrollment__user__in=users,
            enrollment__is_active=True,
            **schedule_day_equals_target_day_filter
        ).order_by(order_by)

        if self.org_list is not None:
            if self.exclude_orgs:
                schedules = schedules.exclude(enrollment__course__org__in=self.org_list)
            else:
                schedules = schedules.filter(enrollment__course__org__in=self.org_list)

        if "read_replica" in settings.DATABASES:
            schedules = schedules.using("read_replica")

        LOG.debug('Query = %r', schedules.query.sql_with_params())

        with function_trace('schedule_query_set_evaluation'):
            # This will run the query and cache all of the results in memory.
            num_schedules = len(schedules)

        # This should give us a sense of the volume of data being processed by each task.
        set_custom_metric('num_schedules', num_schedules)

        return schedules

    def schedules_for_bin(self):
        schedules = self.get_schedules_with_target_date_by_bin_and_orgs()
        template_context = get_base_template_context(self.site)

        for (user, user_schedules) in groupby(schedules, lambda s: s.enrollment.user):
            user_schedules = list(user_schedules)
            course_id_strs = [str(schedule.enrollment.course_id) for schedule in user_schedules]

            # This is used by the bulk email optout policy
            template_context['course_ids'] = course_id_strs

            first_schedule = user_schedules[0]
            template_context.update(self.get_template_context(user, user_schedules))

            # Information for including upsell messaging in template.
            _add_upsell_button_information_to_template_context(
                user, first_schedule, template_context)

            yield (user, first_schedule.enrollment.course.language, template_context)

    def get_template_context(self, user, user_schedules):
        """
        Given a user and their schedules, build the context needed to render the template for this message.

        Arguments:
             user -- the User who will be receiving the message
             user_schedules -- a list of Schedule objects representing all of their schedules that should be covered by
                               this message. For example, when a user enrolls in multiple courses on the same day, we
                               don't want to send them multiple reminder emails. Instead this list would have multiple
                               elements, allowing us to send a single message for all of the courses.

        Returns:
            dict: This dict must be JSON serializable (no datetime objects!). When rendering the message templates it
                  it will be used as the template context. Note that it will also include several default values that
                  injected into all template contexts. See `get_base_template_context` for more information.
        """
        return {}


class ScheduleStartResolver(BinnedSchedulesBaseResolver):
    """
    Send a message to all users whose schedule started at ``self.current_date`` + ``day_offset``.
    """
    log_prefix = 'Scheduled Nudge'
    schedule_date_field = 'start'
    num_bins = RECURRING_NUDGE_NUM_BINS

    def get_template_context(self, user, user_schedules):
        first_schedule = user_schedules[0]
        return {
            'course_name': first_schedule.enrollment.course.display_name,
            'course_url': absolute_url(
                self.site, reverse('course_root', args=[str(first_schedule.enrollment.course_id)])
            ),
        }


def _get_datetime_beginning_of_day(dt):
    """
    Truncates hours, minutes, seconds, and microseconds to zero on given datetime.
    """
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


class UpgradeReminderResolver(BinnedSchedulesBaseResolver):
    """
    Send a message to all users whose verified upgrade deadline is at ``self.current_date`` + ``day_offset``.
    """
    log_prefix = 'Upgrade Reminder'
    schedule_date_field = 'upgrade_deadline'
    num_bins = UPGRADE_REMINDER_NUM_BINS

    def get_template_context(self, user, user_schedules):
        first_schedule = user_schedules[0]
        return {
            'course_links': [
                {
                    'url': absolute_url(self.site, reverse('course_root', args=[str(s.enrollment.course_id)])),
                    'name': s.enrollment.course.display_name
                } for s in user_schedules
            ],
            'first_course_name': first_schedule.enrollment.course.display_name,
            'cert_image': absolute_url(self.site, static('course_experience/images/verified-cert.png')),
        }


def _add_upsell_button_information_to_template_context(user, schedule, template_context):
    enrollment = schedule.enrollment
    course = enrollment.course

    verified_upgrade_link = _get_verified_upgrade_link(user, schedule)
    has_verified_upgrade_link = verified_upgrade_link is not None

    if has_verified_upgrade_link:
        template_context['upsell_link'] = verified_upgrade_link
        template_context['user_schedule_upgrade_deadline_time'] = dateformat.format(
            enrollment.dynamic_upgrade_deadline,
            get_format(
                'DATE_FORMAT',
                lang=course.language,
                use_l10n=True
            )
        )

    template_context['show_upsell'] = has_verified_upgrade_link


def _get_verified_upgrade_link(user, schedule):
    enrollment = schedule.enrollment
    if enrollment.dynamic_upgrade_deadline is not None and verified_upgrade_link_is_valid(enrollment):
        return verified_upgrade_deadline_link(user, enrollment.course)


class CourseUpdateResolver(BinnedSchedulesBaseResolver):
    """
    Send a message to all users whose schedule started at ``self.current_date`` + ``day_offset`` and the
    course has updates.
    """
    log_prefix = 'Course Update'
    schedule_date_field = 'start'
    num_bins = COURSE_UPDATE_NUM_BINS

    def schedules_for_bin(self):
        week_num = abs(self.day_offset) / 7
        schedules = self.get_schedules_with_target_date_by_bin_and_orgs(
            order_by='enrollment__course',
        )

        template_context = get_base_template_context(self.site)
        for schedule in schedules:
            enrollment = schedule.enrollment
            try:
                week_summary = get_course_week_summary(enrollment.course_id, week_num)
            except CourseUpdateDoesNotExist:
                continue

            user = enrollment.user
            course_id_str = str(enrollment.course_id)

            template_context.update({
                'student_name': user.profile.name,
                'course_name': schedule.enrollment.course.display_name,
                'course_url': absolute_url(
                    self.site, reverse('course_root', args=[str(schedule.enrollment.course_id)])
                ),
                'week_num': week_num,
                'week_summary': week_summary,

                # This is used by the bulk email optout policy
                'course_ids': [course_id_str],
            })

            yield (user, schedule.enrollment.course.language, template_context)


@request_cached
def get_course_week_summary(course_id, week_num):
    if COURSE_UPDATE_WAFFLE_FLAG.is_enabled(course_id):
        course = modulestore().get_course(course_id)
        return course.week_summary(week_num)
    else:
        raise CourseUpdateDoesNotExist()
