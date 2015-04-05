#!/usr/bin/env python
# vim: set fileencoding=utf-8 :

from flask import Flask, render_template, flash, redirect, request, url_for, Response, json
from flask_bootstrap import Bootstrap
from flask_login import login_user, current_user, login_required, logout_user
from model import db, Move, Sample, MoveEdit
from datetime import timedelta, date, datetime
from sqlalchemy.sql import func
from sqlalchemy import distinct, literal
import os
from flask_bcrypt import Bcrypt
import imports
import gpx_export
import csv_export
import dateutil.parser
from flask.helpers import make_response
from flask_script import Manager, Server
from flask_migrate import Migrate, MigrateCommand
from commands import AddUser, ImportMove, DeleteMove, ListMoves
from filters import register_filters, register_globals, radian_to_degree, get_city
from login import login_manager, load_user, LoginForm
import itertools
from collections import OrderedDict
from flask_util_js import FlaskUtilJs
from _import import postprocess_move
from geopy.distance import vincenty
import math
import operator
from jinja2.exceptions import TemplateNotFound
try:
    from urllib.parse import quote_plus
except ImportError:
    from urllib import quote_plus as __quote_plus
    quote_plus = lambda x: __quote_plus(x.encode('utf-8'))


app = Flask('openmoves')
fujs = FlaskUtilJs(app)

app_bcrypt = Bcrypt()
migrate = Migrate(app, db)

register_filters(app)
register_globals(app)


def initialize_config(f):

    random_bytes = os.urandom(32)

    if isinstance(random_bytes[0], str):
        random_bytes = [ord(c) for c in random_bytes]

    data = "SECRET_KEY = '%s'\n" % "".join("{:02x}".format(c) for c in random_bytes)
    f.write(data)


def _sample_to_point(sample):
    return (radian_to_degree(sample.latitude), radian_to_degree(sample.longitude))


def calculate_distances(model, samples):
    total_distance_horizontal = 0.0
    total_distance_real = 0.0
    total_distance_descent = 0.0
    total_distance_ascent = 0.0
    total_distance_flat = 0.0
    previous_gps_sample = None
    current_altitude_sample = None
    previous_altitude_sample = None

    for sample in samples:
        if sample.altitude:
            current_altitude_sample = sample
        if sample.latitude:
            if previous_gps_sample:
                distance_horizontal = vincenty(_sample_to_point(previous_gps_sample), _sample_to_point(sample)).meters
                if previous_altitude_sample:
                    if current_altitude_sample != previous_altitude_sample and distance_horizontal > 0:
                        total_distance_horizontal += distance_horizontal
                        hm = current_altitude_sample.altitude - previous_altitude_sample.altitude
                        distance_real = math.sqrt(distance_horizontal ** 2 + hm ** 2)

                        if hm > 0:
                            total_distance_ascent += distance_real
                        elif hm < 0:
                            total_distance_descent += distance_real
                        else:
                            total_distance_flat += distance_real

                        total_distance_real += distance_real
                        previous_gps_sample = sample
                        previous_altitude_sample = current_altitude_sample
                else:
                    previous_altitude_sample = current_altitude_sample
            else:
                previous_gps_sample = sample

    model['total_distance_horizontal'] = total_distance_horizontal
    model['total_distance_ascent'] = total_distance_ascent
    model['total_distance_descent'] = total_distance_descent
    model['total_distance_flat'] = total_distance_flat
    model['total_distance_real'] = total_distance_real


def init(configfile):
    app.config.from_pyfile('openmoves.cfg.default', silent=False)
    if configfile:
        if not os.path.exists(configfile):
            with open(configfile, 'w') as f:
                initialize_config(f)
            print("created %s" % configfile)

        app.config.from_pyfile(configfile, silent=False)
        assert app.config["SECRET_KEY"]

    db.init_app(app)

    with app.app_context():
        if db.engine.name == 'sqlite':
            db.create_all()

    Bootstrap(app)
    app_bcrypt.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "login"

    return app


def command_app_context():
    app.config.update(SQLALCHEMY_ECHO=False)
    return app.app_context()


manager = Manager(init)

manager.add_option('-c', '--config', dest='configfile', default='openmoves.cfg', required=False)

manager.add_command("runserver", Server(use_debugger=True))
manager.add_command('db', MigrateCommand)
manager.add_command('add-user', AddUser(command_app_context, app_bcrypt))
manager.add_command('import-move', ImportMove(command_app_context))
manager.add_command('delete-move', DeleteMove(command_app_context))
manager.add_command('list-moves', ListMoves(command_app_context))


@app.errorhandler(404)
def error404(error):
    return render_template('_404.html'), 404


@app.route('/import', methods=['GET', 'POST'])
@login_required
def move_import():
    xmlfiles = request.files.getlist('files')
    imported_moves = []

    for xmlfile in xmlfiles:
        filename = xmlfile.filename
        if filename:
            app.logger.info("importing '%s'" % filename)
            move = imports.move_import(xmlfile, filename, current_user)
            if move:
                imported_moves.append(move)

    if imported_moves:
        if len(imported_moves) == 1:
            move = imported_moves[0]
            flash("imported '%s': move %d" % (xmlfile.filename, move.id))
            return redirect(url_for('move', id=move.id))
        else:
            flash("imported %d moves" % len(imported_moves))
            return redirect(url_for('moves', start_date=request.form['start_date'], end_date=request.form['end_date']))
    else:
        return render_template('import.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = load_user(username=form.username.data)
        if not user:
            flash("no such user", 'error')
            return render_template('login.html', form=form)

        if app_bcrypt.check_password_hash(user.password, form.password.data):
            login_user(user)

            return redirect(request.args.get('next') or url_for('moves', start_date=request.form['start_date'], end_date=request.form['end_date']))
        else:
            flash("login failed", 'error')
            return render_template('login.html', form=form)

    return render_template('login.html', form=form)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


@app.route('/')
def index():
    nr_of_moves = Move.query.count()

    return render_template('index.html', nr_of_moves=nr_of_moves)


@app.route('/dashboard')
@login_required
def dashboard():
    if ('start_date' not in request.args) or ('end_date' not in request.args):
        raise ValueError("No start_date and/or end_date specified!")

    model = {}
    model['start_date'] = dateutil.parser.parse(request.args.get('start_date'))
    model['end_date'] = dateutil.parser.parse(request.args.get('end_date'))

    moves = _current_user_filtered(Move.query).filter(Move.date_time >= model['start_date']) \
                                              .filter(Move.date_time <= model['end_date']) \
                                              .all()

    model['nr_of_moves'] = len(moves)

    total_distance_by_activity = {}
    total_duration_by_activity = {}
    total_average_by_activity = {}
    total_ascent_by_activity = {}
    total_descent_by_activity = {}

    for move in moves:
        if not (move.activity in total_distance_by_activity):
            total_distance_by_activity[move.activity] = 0
        total_distance_by_activity[move.activity] += move.distance

        if not move.activity in total_duration_by_activity:
            total_duration_by_activity[move.activity] = 0
        total_duration_by_activity[move.activity] += move.duration.total_seconds()

        if not move.activity in total_ascent_by_activity:
            total_ascent_by_activity[move.activity] = 0
        if move.ascent:
            total_ascent_by_activity[move.activity] += move.ascent

        if not move.activity in total_descent_by_activity:
            total_descent_by_activity[move.activity] = 0
        if move.ascent:
            total_descent_by_activity[move.activity] += move.descent

    # Clean activities without distance or duration
    for activity in list(total_distance_by_activity.keys()):
        if(total_distance_by_activity[activity] == 0):
            del total_distance_by_activity[activity]
    for activity in list(total_duration_by_activity.keys()):
        if(total_duration_by_activity[activity] == timedelta(0)):
            del total_duration_by_activity[activity]
    for activity in list(total_ascent_by_activity.keys()):
        if(total_ascent_by_activity[activity] == 0):
            del total_ascent_by_activity[activity]
    for activity in list(total_descent_by_activity.keys()):
        if(total_descent_by_activity[activity] == 0):
            del total_descent_by_activity[activity]

    # Calculate average speeds
    total_duration_with_distance = 0
    for activity in total_distance_by_activity.keys():
        if activity not in total_duration_by_activity:
            continue
        total_average_by_activity[activity] = total_distance_by_activity[activity] / total_duration_by_activity[activity]
        total_duration_with_distance += total_duration_by_activity[activity]

    # Sort totals by activity
    model['total_distance_by_activity'] = OrderedDict(sorted(total_distance_by_activity.items(), key=operator.itemgetter(1), reverse=True))
    model['total_duration_by_activity'] = OrderedDict(sorted(total_duration_by_activity.items(), key=operator.itemgetter(1), reverse=True))
    model['total_average_by_activity'] = OrderedDict(sorted(total_average_by_activity.items(), key=operator.itemgetter(1), reverse=True))
    model['total_ascent_by_activity'] = OrderedDict(sorted(total_ascent_by_activity.items(), key=operator.itemgetter(1), reverse=True))
    model['total_descent_by_activity'] = OrderedDict(sorted(total_descent_by_activity.items(), key=operator.itemgetter(1), reverse=True))

    # Calculate totals
    model['total_distance'] = sum(total_distance_by_activity.values());
    model['total_duration'] = sum(total_duration_by_activity.values(), 0);
    model['total_average'] = model['total_distance'] / total_duration_with_distance if total_duration_with_distance > 0 else None;
    model['total_ascent'] = sum(total_ascent_by_activity.values());
    model['total_descent'] = sum(total_descent_by_activity.values());

    return render_template('dashboard.html', **model)

def _parse_move_filter(filter_query):
    if not filter_query:
        return lambda query: query

    filter_parts = [part.strip() for part in filter_query.split(':')]
    if len(filter_parts) != 2 or filter_parts[0] not in ('activity'):
        flash("illegal filter: '%s'" % filter_query, 'error')
        return lambda query: query
    else:
        filter_attr = getattr(Move, filter_parts[0])
        filter_value = filter_parts[1]
        return lambda query: query.filter(filter_attr == filter_value)


def _current_user_filtered(query):
    return query.filter_by(user=current_user)


@app.route('/moves')
@login_required
def moves():
    if ('start_date' not in request.args) or ('end_date' not in request.args):
        raise ValueError("No start_date and/or end_date specified!")

    start_date = dateutil.parser.parse(request.args.get('start_date'))
    end_date = dateutil.parser.parse(request.args.get('end_date'))

    moves = _current_user_filtered(Move.query).filter(Move.date_time >= start_date) \
                                              .filter(Move.date_time <= end_date)

    total_moves_count = moves.count()
    move_filter = _parse_move_filter(request.args.get('filter'))
    moves = move_filter(moves)

    sort = request.args.get('sort')
    sort_order = request.args.get('sort_order')
    sort_default = 'date_time'
    if not sort:
        sort = sort_default
        sort_order = 'desc'
    if not sort_order:
        sort_order = 'asc'

    if not hasattr(Move, sort):
        flash("illegal sort field: %s" % sort, 'error')
        sort = sort_default

    activity_counts = OrderedDict(_current_user_filtered(db.session.query(Move.activity, func.count(Move.id)))
                                  .filter(Move.date_time >= start_date)
                                  .filter(Move.date_time <= end_date)
                                  .group_by(Move.activity)
                                  .order_by(func.count(Move.id).desc()))

    actual_activities_query = move_filter(_current_user_filtered(db.session.query(distinct(Move.activity))))
    actual_activities = set([activity for activity, in actual_activities_query])

    sort_attr = getattr(Move, sort)
    if not sort_order or sort_order == 'asc':
        sort_attr = sort_attr.asc()
    else:
        sort_attr = sort_attr.desc()

    if db.engine.name == "postgresql":
        sort_attr = sort_attr.nullslast()

    show_columns = {}
    for column in ('location_address', 'speed_avg', 'speed_max', 'hr_avg', 'ascent', 'descent', 'recovery_time', 'stroke_count', 'pool_length'):
        attr = getattr(Move, column)
        exists = db.session.query(literal(True)).filter(move_filter(_current_user_filtered(db.session.query(attr).filter(attr != None))).exists()).scalar()
        show_columns[column] = exists

    moves = moves.order_by(sort_attr)
    return render_template('moves.html',
                           start_date=start_date,
                           end_date=end_date,
                           moves=moves,
                           total_moves_count=total_moves_count,
                           activity_counts=activity_counts,
                           actual_activities=actual_activities,
                           show_columns=show_columns,
                           sort=sort,
                           sort_order=sort_order)


@app.route('/moves/<int:id>/delete')
@login_required
def delete_move(id):
    move = _current_user_filtered(Move.query).filter_by(id=id).first_or_404()
    Sample.query.filter_by(move=move).delete(synchronize_session=False)
    MoveEdit.query.filter_by(move=move).delete(synchronize_session=False)
    db.session.delete(move)
    db.session.commit()
    flash("move %d deleted" % id, 'success')

    return redirect(url_for('moves', start_date=request.args.get('start_date'), end_date=request.args.get('end_date')))


@app.route('/moves/<int:id>/export')
@login_required
def export_move(id):
    move = _current_user_filtered(Move.query).filter_by(id=id).first_or_404()

    if "format" in request.args:
        format = request.args.get("format").lower()
    else:
        format = "gpx"  # default

    format_handlers = {}
    format_handlers['gpx'] = gpx_export.gpx_export
    format_handlers['csv'] = csv_export.csv_export
    if format not in format_handlers:
        flash("Export format %s not supported" % format, 'error')
        return redirect(url_for('move', id=id))

    export_file = format_handlers[format](move)

    if not export_file:
        return redirect(url_for('move', id=id))

    # app.logger.debug("Move export (format %s):\n%s" % (format, export_file))
    response = make_response(export_file)
    date_time = move.date_time.strftime('%Y-%m-%dT%H_%M_%S')
    if move.location_raw:
        address = move.location_raw['address']
        city = get_city(address)
        country_code = address['country_code'].upper()
        filename = "Move_%s_%s_%s_%s.%s" % (date_time, country_code, city, move.activity, format)
    else:
        filename = "Move_%s_%s.%s" % (date_time, move.activity, format)

    response.headers['Content-Disposition'] = "attachment; filename=%s" % (quote_plus(filename))
    return response


@app.route('/moves/<int:id>', methods=['POST'])
@login_required
def edit_move(id):
    name = request.form.get('name')
    pk = request.form.get('pk')
    value = request.form.get('value')
    assert id == int(pk)

    move = _current_user_filtered(Move.query).filter_by(id=id).first_or_404()

    if name == 'activity':
        result = db.session.query(Move.activity_type).filter(Move.activity == value).first()
        if not result:
            raise ValueError("illegal value: %s" % value)

        activity_type, = result

        move_edit = MoveEdit()
        move_edit.date_time = datetime.now()
        move_edit.move = move
        move_edit.old_value = {'activity': move.activity, 'activity_type': move.activity_type}
        move_edit.new_value = {'activity': value, 'activity_type': activity_type}

        db.session.add(move_edit)

        move.activity_type = activity_type
        move.activity = value

        db.session.commit()
    else:
        raise ValueError("illegal name: %s" % name)

    return "OK"


@app.route('/activity_types')
@login_required
def activity_types():
    activities = db.session.query(Move.activity).group_by(Move.activity).order_by(Move.activity.asc())
    data = [{'value': activity, 'text': activity} for activity, in activities]
    return Response(json.dumps(data), mimetype='application/json')


@app.route('/moves/<int:id>', methods=['GET'])
@login_required
def move(id):
    move = _current_user_filtered(Move.query).filter_by(id=id).first_or_404()

    samples = move.samples.order_by('time asc').all()
    events = [sample for sample in samples if sample.events]

    filtered_events = []
    pauses = []
    laps = []
    pause_begin = None
    for sample in events:
        assert len(sample.events.keys()) == 1
        if 'pause' in sample.events:
            state = sample.events['pause']['state'].lower() == 'true'
            if state:
                pause_begin = sample
            elif not state and pause_begin:
                pauses.append([pause_begin, sample])
        elif 'lap' in sample.events:
            laps.append(sample)
        else:
            filtered_events.append(sample)

    model = {}
    model['move'] = move
    model['samples'] = samples
    model['events'] = filtered_events
    model['pauses'] = pauses
    model['laps'] = laps

    gps_samples = [sample for sample in samples if sample.sample_type and sample.sample_type.startswith('gps-')]
    model['gps_samples'] = gps_samples

    if gps_samples:
        if not move.gps_center_max_distance:
            postprocess_move(move)
            flash(u"got %d GPS samples but no center. recalculated" % len(gps_samples), u'warning')
            db.session.commit()

        gps_center_max_distance = move.gps_center_max_distance

        # empirically determined values
        if gps_center_max_distance < 2000:
            map_zoom_level = 14
        elif gps_center_max_distance < 4000:
            map_zoom_level = 13
        elif gps_center_max_distance < 7500:
            map_zoom_level = 12
        elif gps_center_max_distance < 10000:
            map_zoom_level = 11
        else:
            map_zoom_level = 10

        calculate_distances(model, move.samples)

        model['map_zoom_level'] = map_zoom_level

    if 'swimming' in move.activity:
        swimming_events = [sample for sample in filtered_events if 'swimming' in sample.events]
        model['swimming_events'] = swimming_events

        model['swimming_style_changes'] = [sample for sample in swimming_events if sample.events['swimming']['type'] == 'StyleChange']
        model['swimming_turns'] = [sample for sample in swimming_events if sample.events['swimming']['type'] == 'Turn']

        swimming_strokes = [sample for sample in swimming_events if sample.events['swimming']['type'] == 'Stroke']
        model['swimming_strokes'] = swimming_strokes

        pause_samples = list(itertools.chain.from_iterable(pauses))
        model['swimming_strokes_and_pauses'] = sorted(swimming_strokes + pause_samples, key=lambda sample: sample.time)

        model['swim_pace'] = timedelta(seconds=move.duration.total_seconds() / move.distance)

        assert len(model['swimming_strokes']) == move.stroke_count

    # eg. 'Pool swimming' → 'pool_swimming'
    activity_name = move.activity.lower().replace(' ', '_')
    try:
        return render_template("move/%s.html" % activity_name, **model)
    except TemplateNotFound:
        # Fall-back to generic template
        return render_template("move/_move.html", **model)
    except:
        flash("Failed to load move template of activity '%s'." % activity_name)
        return redirect(url_for('index'))

@app.route('/_tests', methods=['GET'])
@login_required
def tests():
    return render_template('tests.html')

if __name__ == '__main__':
    manager.run()
