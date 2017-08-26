# -*- coding: utf8 -*-
# This file is part of PYBOSSA.
#
# Copyright (C) 2015 Scifabric LTD.
#
# PYBOSSA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PYBOSSA is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with PYBOSSA.  If not, see <http://www.gnu.org/licenses/>.
"""Jobs module for running background tasks in PYBOSSA server."""
from datetime import datetime
import math
import requests
from flask import current_app, render_template
from flask.ext.mail import Message
from pybossa.core import mail, task_repo, importer, create_app
from pybossa.model.webhook import Webhook
from pybossa.util import with_cache_disabled, publish_channel
import pybossa.dashboard.jobs as dashboard
from pybossa.leaderboard.jobs import leaderboard
from pbsonesignal import PybossaOneSignal


def schedule_job(function, scheduler, the_app):
    the_app.logger.error("This isn't really an error, scheduled jobs was triggered!")
    """Schedule a job and return a log message."""
    scheduled_jobs = scheduler.get_jobs()
    the_app.logger.error("1")
    job = scheduler.schedule(
        scheduled_time=(function.get('scheduled_time') or datetime.utcnow()),
        func=function['name'],
        args=function['args'],
        kwargs=function['kwargs'],
        interval=function['interval'],
        repeat=None,
        timeout=function['timeout'])
    the_app.logger.error("2")
    for sj in scheduled_jobs:
        if (function['name'].__name__ in sj.func_name and
            sj.args == function['args'] and
            sj.kwargs == function['kwargs']):
            job.cancel()
            msg = ('WARNING: Job %s(%s, %s) is already scheduled'
                   % (function['name'].__name__, function['args'],
                      function['kwargs']))
            the_app.logger.error(msg)
            return msg
    the_app.logger.error("3")
    msg = ('Scheduled %s(%s, %s) to run every %s seconds'
           % (function['name'].__name__, function['args'], function['kwargs'],
              function['interval']))
    the_app.logger.error("4")
    the_app.logger.error(msg)
    return msg


def get_quarterly_date(now):
    """Get quarterly date."""
    if not isinstance(now, datetime):
        raise TypeError('Expected %s, got %s' % (type(datetime), type(now)))
    execute_month = int(math.ceil(now.month / 3.0) * 3)
    execute_day = 31 if execute_month in [3, 12] else 30
    execute_date = datetime(now.year, execute_month, execute_day)
    return datetime.combine(execute_date, now.time())


def enqueue_job(job):
    """Enqueues a job."""
    from pybossa.core import sentinel
    from rq import Queue
    redis_conn = sentinel.master
    queue = Queue(job['queue'], connection=redis_conn)
    queue.enqueue_call(func=job['name'],
                       args=job['args'],
                       kwargs=job['kwargs'],
                       timeout=job['timeout'])
    return True

def enqueue_periodic_jobs(queue_name):
    current_app.logger.error("This isn't really an error, enqueue periodic jobs was triggered!")

    """Enqueue all PYBOSSA periodic jobs."""
    from pybossa.core import sentinel
    from rq import Queue
    redis_conn = sentinel.master

    jobs_generator = get_periodic_jobs(queue_name)
    n_jobs = 0
    queue = Queue(queue_name, connection=redis_conn)
    for job in jobs_generator:
        if (job['queue'] == queue_name):
            n_jobs += 1
            queue.enqueue_call(func=job['name'],
                               args=job['args'],
                               kwargs=job['kwargs'],
                               timeout=job['timeout'])
    msg = "%s jobs in %s have been enqueued" % (n_jobs, queue_name)
    return msg


def get_periodic_jobs(queue):
    current_app.logger.error("This isn't really an error, get periodic jobs was triggered!")

    """Return a list of periodic jobs for a given queue."""
    # A job is a dict with the following format: dict(name, args, kwargs,
    # timeout, queue)
    # Default ones
    jobs = get_default_jobs()
    # Create ZIPs for all projects
    zip_jobs = get_export_task_jobs(queue) if queue in ('high', 'low') else []
    # Based on type of user
    project_jobs = get_project_jobs(queue) if queue in ('super', 'high') else []
    autoimport_jobs = get_autoimport_jobs() if queue == 'maintenance' else []
    # User engagement jobs
    engage_jobs = get_inactive_users_jobs() if queue == 'quaterly' else []
    non_contrib_jobs = get_non_contributors_users_jobs() \
        if queue == 'quaterly' else []
    dashboard_jobs = get_dashboard_jobs() if queue == 'low' else []
    leaderboard_jobs = get_leaderboard_jobs() if queue == 'super' else []
    weekly_update_jobs = get_weekly_stats_update_projects() if queue == 'low' else []
    failed_jobs = get_maintenance_jobs() if queue == 'maintenance' else []
    _all = [zip_jobs, jobs, project_jobs, autoimport_jobs,
            engage_jobs, non_contrib_jobs, dashboard_jobs,
            weekly_update_jobs, failed_jobs, leaderboard_jobs]
    return (job for sublist in _all for job in sublist if job['queue'] == queue)


def get_default_jobs():  # pragma: no cover
    """Return default jobs."""
    timeout = current_app.config.get('TIMEOUT')
    yield dict(name=warm_up_stats, args=[], kwargs={},
               timeout=timeout, queue='high')
    yield dict(name=warn_old_project_owners, args=[], kwargs={},
               timeout=timeout, queue='low')
    yield dict(name=warm_cache, args=[], kwargs={},
               timeout=timeout, queue='super')
    yield dict(name=news, args=[], kwargs={},
               timeout=timeout, queue='low')

def get_maintenance_jobs():
    """Return mantainance jobs."""
    timeout = current_app.config.get('TIMEOUT')
    yield dict(name=check_failed, args=[], kwargs={},
               timeout=timeout, queue='maintenance')


def get_export_task_jobs(queue):
    """Export tasks to zip."""
    from pybossa.core import project_repo
    import pybossa.cache.projects as cached_projects
    from pybossa.pro_features import ProFeatureHandler
    feature_handler = ProFeatureHandler(current_app.config.get('PRO_FEATURES'))
    timeout = current_app.config.get('TIMEOUT')
    if feature_handler.only_for_pro('updated_exports'):
        if queue == 'high':
            projects = cached_projects.get_from_pro_user()
        else:
            projects = (p.dictize() for p in project_repo.filter_by(published=True)
                        if p.owner.pro is False)
    else:
        projects = (p.dictize() for p in project_repo.filter_by(published=True))
    for project in projects:
        project_id = project.get('id')
        job = dict(name=project_export,
                   args=[project_id], kwargs={},
                   timeout=timeout,
                   queue=queue)
        yield job


def project_export(_id):
    """Export project."""
    from pybossa.core import project_repo, json_exporter, csv_exporter
    app = project_repo.get(_id)
    if app is not None:
        print "Export project id %d" % _id
        json_exporter.pregenerate_zip_files(app)
        csv_exporter.pregenerate_zip_files(app)


def get_project_jobs(queue):
    """Return a list of jobs based on user type."""
    from pybossa.core import project_repo
    from pybossa.cache import projects as cached_projects
    timeout = current_app.config.get('TIMEOUT')
    if queue == 'super':
        projects = cached_projects.get_from_pro_user()
    elif queue == 'high':
        projects = (p.dictize() for p in project_repo.filter_by(published=True)
                    if p.owner.pro is False)
    else:
        projects = []
    for project in projects:
        project_id = project.get('id')
        project_short_name = project.get('short_name')
        job = dict(name=get_project_stats,
                   args=[project_id, project_short_name], kwargs={},
                   timeout=timeout,
                   queue=queue)
        yield job


def create_dict_jobs(data, function, timeout, queue='low'):
    """Create a dict job."""
    for d in data:
        jobs = dict(name=function,
                    args=[d['id'], d['short_name']], kwargs={},
                    timeout=timeout,
                    queue=queue)
        yield jobs


def get_inactive_users_jobs(queue='quaterly'):
    """Return a list of inactive users that have contributed to a project."""
    from sqlalchemy.sql import text
    from pybossa.model.user import User
    from pybossa.core import db
    # First users that have participated once but more than 3 months ago
    sql = text('''SELECT user_id FROM task_run
               WHERE user_id IS NOT NULL
               AND to_date(task_run.finish_time, 'YYYY-MM-DD\THH24:MI:SS.US')
               >= NOW() - '12 month'::INTERVAL
               AND to_date(task_run.finish_time, 'YYYY-MM-DD\THH24:MI:SS.US')
               < NOW() - '3 month'::INTERVAL
               GROUP BY user_id ORDER BY user_id;''')
    results = db.slave_session.execute(sql)

    timeout = current_app.config.get('TIMEOUT')

    for row in results:

        user = User.query.get(row.user_id)

        if user.subscribed:
            subject = "We miss you!"
            body = render_template('/account/email/inactive.md',
                                   user=user.dictize(),
                                   config=current_app.config)
            html = render_template('/account/email/inactive.html',
                                   user=user.dictize(),
                                   config=current_app.config)

            mail_dict = dict(recipients=[user.email_addr],
                             subject=subject,
                             body=body,
                             html=html)

            job = dict(name=send_mail,
                       args=[mail_dict],
                       kwargs={},
                       timeout=timeout,
                       queue=queue)
            yield job


def get_dashboard_jobs(queue='low'):  # pragma: no cover
    """Return dashboard jobs."""
    timeout = current_app.config.get('TIMEOUT')
    yield dict(name=dashboard.active_users_week, args=[], kwargs={},
               timeout=timeout, queue=queue)
    yield dict(name=dashboard.active_anon_week, args=[], kwargs={},
               timeout=timeout, queue=queue)
    yield dict(name=dashboard.draft_projects_week, args=[], kwargs={},
               timeout=timeout, queue=queue)
    yield dict(name=dashboard.published_projects_week, args=[], kwargs={},
               timeout=timeout, queue=queue)
    yield dict(name=dashboard.update_projects_week, args=[], kwargs={},
               timeout=timeout, queue=queue)
    yield dict(name=dashboard.new_tasks_week, args=[], kwargs={},
               timeout=timeout, queue=queue)
    yield dict(name=dashboard.new_task_runs_week, args=[], kwargs={},
               timeout=timeout, queue=queue)
    yield dict(name=dashboard.new_users_week, args=[], kwargs={},
               timeout=timeout, queue=queue)
    yield dict(name=dashboard.returning_users_week, args=[], kwargs={},
               timeout=timeout, queue=queue)


def get_leaderboard_jobs(queue='super'):  # pragma: no cover
    """Return leaderboard jobs."""
    timeout = current_app.config.get('TIMEOUT')
    yield dict(name=leaderboard, args=[], kwargs={},
               timeout=timeout, queue=queue)


def get_non_contributors_users_jobs(queue='quaterly'):
    """Return a list of users that have never contributed to a project."""
    from sqlalchemy.sql import text
    from pybossa.model.user import User
    from pybossa.core import db
    # Second users that have created an account but never participated
    sql = text('''SELECT id FROM "user" WHERE
               NOT EXISTS (SELECT user_id FROM task_run
               WHERE task_run.user_id="user".id)''')
    results = db.slave_session.execute(sql)
    timeout = current_app.config.get('TIMEOUT')
    for row in results:
        user = User.query.get(row.id)

        if user.subscribed:
            subject = "Why don't you help us?!"
            body = render_template('/account/email/noncontributors.md',
                                   user=user.dictize(),
                                   config=current_app.config)
            html = render_template('/account/email/noncontributors.html',
                                   user=user.dictize(),
                                   config=current_app.config)
            mail_dict = dict(recipients=[user.email_addr],
                             subject=subject,
                             body=body,
                             html=html)

            job = dict(name=send_mail,
                       args=[mail_dict],
                       kwargs={},
                       timeout=timeout,
                       queue=queue)
            yield job


def get_autoimport_jobs(queue='low'):
    current_app.logger.error("This isn't really an error, autoimport was triggered!")
    """Get autoimport jobs."""
    from pybossa.core import project_repo
    current_app.logger.error("v1a")
    import pybossa.cache.projects as cached_projects
    current_app.logger.error("v1b")
    from pybossa.pro_features import ProFeatureHandler
    current_app.logger.error("v1c")
    feature_handler = ProFeatureHandler(current_app.config.get('PRO_FEATURES'))

    timeout = current_app.config.get('TIMEOUT')
    current_app.logger.error("v2")
    #if feature_handler.only_for_pro('autoimporter'):
    #    current_app.logger.error("v3a")
    #    projects = cached_projects.get_from_pro_user()
    #else:
    current_app.logger.error("v3b")
    projects = (p.dictize() for p in project_repo.get_all())
    current_app.logger.error("v4")
    current_app.logger.error(str(len(project_repo.get_all())))
    current_app.logger.error(str(len(projects)))
    current_app.logger.error(str(type(projects)))
    current_app.logger.error(str(projects))
    for project_dict in projects:
        current_app.logger.error("v5")
        project = project_repo.get(project_dict['id'])
        if project.has_autoimporter():
            current_app.logger.error("v6")
            job = dict(name=import_tasks,
                       args=[project.id, True],
                       kwargs=project.get_autoimporter(),
                       timeout=timeout,
                       queue=queue)
            current_app.logger.error("v7")
            yield job


# The following are the actual jobs (i.e. tasks performed in the background)

@with_cache_disabled
def get_project_stats(_id, short_name):  # pragma: no cover
    """Get stats for project."""
    import pybossa.cache.projects as cached_projects
    import pybossa.cache.project_stats as stats
    from flask import current_app

    cached_projects.get_project(short_name)
    stats.update_stats(_id, current_app.config.get('GEO'))


@with_cache_disabled
def warm_up_stats():  # pragma: no cover
    """Background job for warming stats."""
    print "Running on the background warm_up_stats"
    from pybossa.cache.site_stats import (n_auth_users, n_anon_users,
                                          n_tasks_site, n_total_tasks_site,
                                          n_task_runs_site,
                                          get_top5_projects_24_hours,
                                          get_top5_users_24_hours, get_locs)
    n_auth_users()
    n_anon_users()
    n_tasks_site()
    n_total_tasks_site()
    n_task_runs_site()
    get_top5_projects_24_hours()
    get_top5_users_24_hours()
    get_locs()

    return True


@with_cache_disabled
def warm_cache():  # pragma: no cover
    """Background job to warm cache."""
    from pybossa.core import create_app
    app = create_app(run_as_server=False)
    projects_cached = []
    import pybossa.cache.projects as cached_projects
    import pybossa.cache.categories as cached_cat
    import pybossa.cache.users as cached_users
    import pybossa.cache.project_stats as stats
    from pybossa.util import rank
    from pybossa.core import user_repo

    def warm_project(_id, short_name, featured=False):
        if _id not in projects_cached:
            cached_projects.get_project(short_name)
            #cached_projects.n_tasks(_id)
            #n_task_runs = cached_projects.n_task_runs(_id)
            #cached_projects.overall_progress(_id)
            #cached_projects.last_activity(_id)
            #cached_projects.n_completed_tasks(_id)
            #cached_projects.n_volunteers(_id)
            #cached_projects.browse_tasks(_id)
            #if n_task_runs >= 1000 or featured:
            #    # print ("Getting stats for %s as it has %s task runs" %
            #    #        (short_name, n_task_runs))
            stats.update_stats(_id, app.config.get('GEO'))
            projects_cached.append(_id)

    # Cache top projects
    projects = cached_projects.get_top()
    for p in projects:
        warm_project(p['id'], p['short_name'])

    # Cache 3 pages
    to_cache = 3 * app.config['APPS_PER_PAGE']
    projects = rank(cached_projects.get_all_featured('featured'))[:to_cache]
    for p in projects:
        warm_project(p['id'], p['short_name'], featured=True)

    # Categories
    categories = cached_cat.get_used()
    for c in categories:
        projects = rank(cached_projects.get_all(c['short_name']))[:to_cache]
        for p in projects:
            warm_project(p['id'], p['short_name'])
    # Users
    users = cached_users.get_leaderboard(app.config['LEADERBOARD'])
    for user in users:
        # print "Getting stats for %s" % user['name']
        print user_repo
        u = user_repo.get_by_name(user['name'])
        cached_users.get_user_summary(user['name'])
        cached_users.projects_contributed_cached(u.id)
        cached_users.published_projects_cached(u.id)
        cached_users.draft_projects_cached(u.id)

    return True


def get_non_updated_projects():
    """Return a list of non updated projects excluding completed ones."""
    from sqlalchemy.sql import text
    from pybossa.model.project import Project
    from pybossa.core import db
    sql = text('''SELECT id FROM project WHERE TO_DATE(updated,
                'YYYY-MM-DD\THH24:MI:SS.US') <= NOW() - '3 month':: INTERVAL
               AND contacted != True AND published = True
               AND project.id NOT IN
               (SELECT task.project_id FROM task
               WHERE task.state='completed'
               GROUP BY task.project_id)''')
    results = db.slave_session.execute(sql)
    projects = []
    for row in results:
        a = Project.query.get(row.id)
        projects.append(a)
    return projects


def warn_old_project_owners():
    """E-mail the project owners not updated in the last 3 months."""
    from pybossa.core import mail, project_repo
    from pybossa.cache.projects import clean
    from flask.ext.mail import Message

    projects = get_non_updated_projects()

    with mail.connect() as conn:
        for project in projects:
            subject = ('Your %s project: %s has been inactive'
                       % (current_app.config.get('BRAND'), project.name))
            body = render_template('/account/email/inactive_project.md',
                                   project=project)
            html = render_template('/account/email/inactive_project.html',
                                   project=project)
            msg = Message(recipients=[project.owner.email_addr],
                          subject=subject,
                          body=body,
                          html=html)
            conn.send(msg)
            project.contacted = True
            project.published = False
            clean(project.id)
            project_repo.update(project)
    return True


def send_mail(message_dict):
    """Send email."""
    message = Message(**message_dict)
    mail.send(message)


def import_tasks(project_id, from_auto=False, **form_data):
    """Import tasks for a project."""
    from pybossa.core import project_repo
    project = project_repo.get(project_id)
    report = importer.create_tasks(task_repo, project_id, **form_data)
    if from_auto:
        form_data['last_import_meta'] = report.metadata
        project.set_autoimporter(form_data)
        project_repo.save(project)
    msg = report.message + ' to your project %s!' % project.name
    subject = 'Tasks Import to your project %s' % project.name
    body = 'Hello,\n\n' + msg + '\n\nAll the best,\nThe %s team.'\
        % current_app.config.get('BRAND')
    mail_dict = dict(recipients=[project.owner.email_addr],
                     subject=subject, body=body)
    send_mail(mail_dict)
    return msg


def webhook(url, payload=None, oid=None):
    """Post to a webhook."""
    from flask import current_app
    from readability.readability import Document
    try:
        import json
        from pybossa.core import sentinel, webhook_repo, project_repo
        project = project_repo.get(payload['project_id'])
        headers = {'Content-type': 'application/json', 'Accept': 'text/plain'}
        if oid:
            webhook = webhook_repo.get(oid)
        else:
            webhook = Webhook(project_id=payload['project_id'],
                              payload=payload)
        if url:
            response = requests.post(url, data=json.dumps(payload), headers=headers)
            webhook.response = Document(response.text).summary()
            webhook.response_status_code = response.status_code
        else:
            raise requests.exceptions.ConnectionError('Not URL')
        if oid:
            webhook_repo.update(webhook)
            webhook = webhook_repo.get(oid)
        else:
            webhook_repo.save(webhook)
    except requests.exceptions.ConnectionError:
        webhook.response = 'Connection Error'
        webhook.response_status_code = None
        webhook_repo.save(webhook)
    finally:
        if project.published and webhook.response_status_code != 200 and current_app.config.get('ADMINS'):
            subject = "Broken: %s webhook failed" % project.name
            body = 'Sorry, but the webhook failed'
            mail_dict = dict(recipients=current_app.config.get('ADMINS'),
                             subject=subject, body=body, html=webhook.response)
            send_mail(mail_dict)
    if current_app.config.get('SSE'):
        publish_channel(sentinel, payload['project_short_name'],
                        data=webhook.dictize(), type='webhook',
                        private=True)
    return webhook


def notify_blog_users(blog_id, project_id, queue='high'):
    """Send email with new blog post."""
    from sqlalchemy.sql import text
    from pybossa.core import db
    from pybossa.core import blog_repo
    from pybossa.pro_features import ProFeatureHandler

    blog = blog_repo.get(blog_id)
    users = 0
    feature_handler = ProFeatureHandler(current_app.config.get('PRO_FEATURES'))
    only_pros = feature_handler.only_for_pro('notify_blog_updates')
    timeout = current_app.config.get('TIMEOUT')
    if blog.project.featured or (blog.project.owner.pro or not only_pros):
        sql = text('''
                   SELECT email_addr, name from "user", task_run
                   WHERE task_run.project_id=:project_id
                   AND task_run.user_id="user".id
                   AND "user".subscribed=true
                   GROUP BY email_addr, name, subscribed;
                   ''')
        results = db.slave_session.execute(sql, dict(project_id=project_id))
        for row in results:
            subject = "Project Update: %s by %s" % (blog.project.name,
                                                    blog.project.owner.fullname)
            body = render_template('/account/email/blogupdate.md',
                                   user_name=row.name,
                                   blog=blog,
                                   config=current_app.config)
            html = render_template('/account/email/blogupdate.html',
                                   user_name=row.name,
                                   blog=blog,
                                   config=current_app.config)
            mail_dict = dict(recipients=[row.email_addr],
                             subject=subject,
                             body=body,
                             html=html)

            job = dict(name=send_mail,
                       args=[mail_dict],
                       kwargs={},
                       timeout=timeout,
                       queue=queue)
            enqueue_job(job)
            users += 1
    msg = "%s users notified by email" % users
    return msg


def get_weekly_stats_update_projects():
    """Return email jobs with weekly stats update for project owner."""
    from sqlalchemy.sql import text
    from pybossa.core import db
    from pybossa.pro_features import ProFeatureHandler

    feature_handler = ProFeatureHandler(current_app.config.get('PRO_FEATURES'))
    only_pros = feature_handler.only_for_pro('project_weekly_report')
    only_pros_sql = 'AND "user".pro=true' if only_pros else ''
    send_emails_date = current_app.config.get('WEEKLY_UPDATE_STATS')
    today = datetime.today().strftime('%A').lower()
    timeout = current_app.config.get('TIMEOUT')
    if today.lower() == send_emails_date.lower():
        sql = text('''
                   SELECT project.id
                   FROM project, "user", task
                   WHERE "user".id=project.owner_id %s
                   AND "user".subscribed=true
                   AND task.project_id=project.id
                   AND task.state!='completed'
                   UNION
                   SELECT project.id
                   FROM project
                   WHERE project.featured=true;
                   ''' % only_pros_sql)
        results = db.slave_session.execute(sql)
        for row in results:
            job = dict(name=send_weekly_stats_project,
                       args=[row.id],
                       kwargs={},
                       timeout=timeout,
                       queue='low')
            yield job


def send_weekly_stats_project(project_id):
    from pybossa.cache.project_stats import update_stats, get_stats
    from pybossa.core import project_repo
    from datetime import datetime
    project = project_repo.get(project_id)
    if project.owner.subscribed is False:
        return "Owner does not want updates by email"
    update_stats(project_id)
    dates_stats, hours_stats, users_stats = get_stats(project_id,
                                                      geo=True,
                                                      period='1 week')
    subject = "Weekly Update: %s" % project.name

    timeout = current_app.config.get('TIMEOUT')

    # Max number of completed tasks
    n_completed_tasks = 0
    xy = zip(*dates_stats[3]['values'])
    n_completed_tasks = max(xy[1])
    # Most active day
    xy = zip(*dates_stats[0]['values'])
    active_day = [xy[0][xy[1].index(max(xy[1]))], max(xy[1])]
    active_day[0] = datetime.fromtimestamp(active_day[0]/1000).strftime('%A')
    body = render_template('/account/email/weeklystats.md',
                           project=project,
                           dates_stats=dates_stats,
                           hours_stats=hours_stats,
                           users_stats=users_stats,
                           n_completed_tasks=n_completed_tasks,
                           active_day=active_day,
                           config=current_app.config)
    html = render_template('/account/email/weeklystats.html',
                           project=project,
                           dates_stats=dates_stats,
                           hours_stats=hours_stats,
                           users_stats=users_stats,
                           active_day=active_day,
                           n_completed_tasks=n_completed_tasks,
                           config=current_app.config)
    mail_dict = dict(recipients=[project.owner.email_addr],
                     subject=subject,
                     body=body,
                     html=html)

    job = dict(name=send_mail,
               args=[mail_dict],
               kwargs={},
               timeout=timeout,
               queue='high')
    enqueue_job(job)


def news():
    """Get news from different ATOM RSS feeds."""
    import feedparser
    from pybossa.core import sentinel
    from pybossa.news import get_news, notify_news_admins, FEED_KEY
    try:
        import cPickle as pickle
    except ImportError:  # pragma: no cover
        import pickle
    urls = ['https://github.com/pybossa/pybossa/releases.atom',
            'http://scifabric.com/blog/all.atom.xml']
    score = 0
    notify = False
    if current_app.config.get('NEWS_URL'):
        urls += current_app.config.get('NEWS_URL')
    for url in urls:
        d = feedparser.parse(url)
        tmp = get_news(score)
        if (len(tmp) == 0) or (tmp[0]['updated'] != d.entries[0]['updated']):
            sentinel.master.zadd(FEED_KEY, float(score),
                                 pickle.dumps(d.entries[0]))
            notify = True
        score += 1
    if notify:
        notify_news_admins()

def check_failed():
    """Check the jobs that have failed and requeue them."""
    from rq import Queue, get_failed_queue, requeue_job
    from pybossa.core import sentinel

    fq = get_failed_queue()
    job_ids = fq.job_ids
    count = len(job_ids)
    FAILED_JOBS_RETRIES = current_app.config.get('FAILED_JOBS_RETRIES')
    for job_id in job_ids:
        KEY = 'pybossa:job:failed:%s' % job_id
        job = fq.fetch_job(job_id)
        if sentinel.slave.exists(KEY):
            sentinel.master.incr(KEY)
        else:
            ttl = current_app.config.get('FAILED_JOBS_MAILS')*24*60*60
            sentinel.master.setex(KEY, ttl, 1)
        if int(sentinel.slave.get(KEY)) < FAILED_JOBS_RETRIES:
            requeue_job(job_id)
        else:
            KEY = 'pybossa:job:failed:mailed:%s' % job_id
            if (not sentinel.slave.exists(KEY) and
                    current_app.config.get('ADMINS')):
                subject = "JOB: %s has failed more than 3 times" % job_id
                body = "Please, review the background jobs of your server."
                body += "\n This is the trace error\n\n"
                body += "------------------------------\n\n"
                body += job.exc_info
                mail_dict = dict(recipients=current_app.config.get('ADMINS'),
                                 subject=subject, body=body)
                send_mail(mail_dict)
                ttl = current_app.config.get('FAILED_JOBS_MAILS')*24*60*60
                sentinel.master.setex(KEY, ttl, True)
    if count > 0:
        return "JOBS: %s You have failed the system." % job_ids
    else:
        return "You have not failed the system"


def push_notification(project_id, **kwargs):
    """Send push notification."""
    from pybossa.core import project_repo
    project = project_repo.get(project_id)
    if project.info.get('onesignal'):
        app_id = current_app.config.get('ONESIGNAL_APP_ID') 
        api_key = current_app.config.get('ONESIGNAL_API_KEY')
        client = PybossaOneSignal(app_id=app_id, api_key=api_key)
        filters = [{"field": "tag", "key": project_id, "relation": "exists"}]
        return client.push_msg(contents=kwargs['contents'],
                               headings=kwargs['headings'],
                               launch_url=kwargs['launch_url'],
                               web_buttons=kwargs['web_buttons'],
                               filters=filters)
