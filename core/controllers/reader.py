# Copyright 2014 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Controllers for the Oppia learner view."""

__author__ = 'Sean Lip'

from core.controllers import base
from core.domain import dependency_registry
from core.domain import event_services
from core.domain import exp_services
from core.domain import feedback_services
from core.domain import rights_manager
from core.domain import skins_services
from core.domain import widget_registry
import feconf
import jinja_utils
import utils

import jinja2


def require_playable(handler):
    """Decorator that checks if the user can play the given exploration."""
    def test_can_play(self, exploration_id, **kwargs):
        """Checks if the user for the current session is logged in."""
        if rights_manager.Actor(self.user_id).can_play(exploration_id):
            return handler(self, exploration_id, **kwargs)
        else:
            raise self.PageNotFoundException

    return test_can_play


class ExplorationPage(base.BaseHandler):
    """Page describing a single exploration."""

    @require_playable
    def get(self, exploration_id):
        """Handles GET requests."""
        version = self.request.get('v')
        if not version:
            # The default value for a missing parameter seems to be ''.
            version = None
        else:
            version = int(version)

        try:
            exploration = exp_services.get_exploration_by_id(
                exploration_id, version=version)
        except Exception as e:
            raise self.PageNotFoundException(e)

        if not rights_manager.Actor(self.user_id).can_view(exploration_id):
            raise self.PageNotFoundException

        is_iframed = (self.request.get('iframed') == 'true')

        # TODO(sll): Cache these computations.
        interactive_widget_ids = exploration.get_interactive_widget_ids()
        widget_dependency_ids = (
            widget_registry.Registry.get_deduplicated_dependency_ids(
                interactive_widget_ids))
        widget_dependencies_html, additional_angular_modules = (
            dependency_registry.Registry.get_deps_html_and_angular_modules(
                widget_dependency_ids))

        widget_js_directives = (
            widget_registry.Registry.get_noninteractive_widget_html() +
            widget_registry.Registry.get_interactive_widget_html(
                interactive_widget_ids))

        self.values.update({
            'additional_angular_modules': additional_angular_modules,
            'exploration_version': version,
            'iframed': is_iframed,
            'is_private': rights_manager.is_exploration_private(
                exploration_id),
            'nav_mode': feconf.NAV_MODE_EXPLORE,
            'skin_html': skins_services.Registry.get_skin_html(
                exploration.default_skin),
            'skin_js_url': skins_services.Registry.get_skin_js_url(
                    exploration.default_skin),
            'skin_tag': jinja2.utils.Markup(
                skins_services.Registry.get_skin_tag(exploration.default_skin)
            ),
            'widget_dependencies_html': jinja2.utils.Markup(
                widget_dependencies_html),
            'widget_js_directives': jinja2.utils.Markup(widget_js_directives),
        })

        if is_iframed:
            self.render_template(
                'player/exploration_player.html', iframe_restriction=None)
        else:
            self.render_template('player/exploration_player.html')


class ExplorationHandler(base.BaseHandler):
    """Provides the initial data for a single exploration."""

    def get(self, exploration_id):
        """Populates the data on the individual exploration page."""
        # TODO(sll): Maybe this should send a complete state machine to the
        # frontend, and all interaction would happen client-side?
        version = self.request.get('v')
        version = int(version) if version else None

        try:
            exploration = exp_services.get_exploration_by_id(
                exploration_id, version=version)
        except Exception as e:
            raise self.PageNotFoundException(e)

        init_params = exploration.get_init_params()
        reader_params = exploration.update_with_state_params(
            exploration.init_state_name, init_params)

        init_state = exploration.init_state
        session_id = utils.generate_random_string(24)

        self.values.update({
            'exploration': exploration.to_player_dict(),
            'is_logged_in': bool(self.user_id),
            'init_html': init_state.content[0].to_html(reader_params),
            'params': reader_params,
            'session_id': session_id,
            'state_name': exploration.init_state_name,
        })
        self.render_json(self.values)

        event_services.StateHitEventHandler.record(
            exploration_id, exploration.init_state_name, True)
        event_services.StartExplorationEventHandler.record(
            exploration_id, version, exploration.init_state_name,
            session_id, reader_params, feconf.PLAY_TYPE_NORMAL)


class FeedbackHandler(base.BaseHandler):
    """Handles feedback to readers."""

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    def _append_answer_to_stats_log(
            self, old_state, answer, exploration_id, exploration_version,
            old_state_name, old_params, handler, rule):
        """Append the reader's answer to the statistics log."""
        widget = widget_registry.Registry.get_widget_by_id(
            feconf.INTERACTIVE_PREFIX, old_state.widget.widget_id)

        # TODO(sll): Should this also depend on old_params?
        recorded_answer = widget.get_stats_log_html(
            old_state.widget.customization_args, answer)
        event_services.AnswerSubmissionEventHandler.record(
            exploration_id, exploration_version, old_state_name, handler,
            rule, recorded_answer)

    def _append_content(self, exploration, finished, old_params,
                        new_state_name, state_has_changed):
        """Appends content for the new state to the output variables."""
        if finished:
            return {}, ''

        # Populate new parameters.
        new_params = exploration.update_with_state_params(
            new_state_name, old_params)

        question_html = ''
        if state_has_changed:
            # Append the content for the new state.
            question_html = exploration.states[
                new_state_name].content[0].to_html(new_params)

        return (new_params, question_html)

    @require_playable
    def post(self, exploration_id, escaped_state_name):
        """Handles feedback interactions with readers."""
        old_state_name = self.unescape_state_name(escaped_state_name)
        # The reader's answer.
        answer = self.payload.get('answer')
        # The answer handler (submit, click, etc.)
        handler = self.payload.get('handler')
        # Parameters associated with the reader.
        old_params = self.payload.get('params', {})
        old_params['answer'] = answer
        # The version of the exploration.
        version = self.payload.get('version')

        values = {}
        exploration = exp_services.get_exploration_by_id(
            exploration_id, version=version)
        old_state = exploration.states[old_state_name]
        old_widget = widget_registry.Registry.get_widget_by_id(
            feconf.INTERACTIVE_PREFIX, old_state.widget.widget_id)

        answer = old_widget.normalize_answer(answer, handler)

        rule = exploration.classify(
            old_state_name, handler, answer, old_params)
        new_state_name = rule.dest
        new_state = (
            None if new_state_name == feconf.END_DEST
            else exploration.states[new_state_name])

        self._append_answer_to_stats_log(
            old_state, answer, exploration_id, exploration.version,
            old_state_name, old_params, handler, rule)

        # Add Oppia's feedback to the response HTML.
        feedback_html = '<div>%s</div>' % jinja_utils.parse_string(
            rule.get_feedback_string(), old_params)

        # Add the content for the new state to the response HTML.
        finished = (new_state_name == feconf.END_DEST)
        state_has_changed = (old_state_name != new_state_name)
        new_params, question_html = self._append_content(
            exploration, finished, old_params, new_state_name,
            state_has_changed)

        values.update({
            'feedback_html': feedback_html,
            'finished': finished,
            'params': new_params,
            'question_html': question_html,
            'state_name': new_state_name,
        })

        self.render_json(values)


class StateHitEventHandler(base.BaseHandler):
    """Tracks a learner hitting a new state."""

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    @require_playable
    def post(self, exploration_id):
        """Handles POST requests."""
        new_state_name = self.payload.get('new_state_name')
        first_time = self.payload.get('first_time')
        exploration_version = self.payload.get('exploration_version')
        session_id = self.payload.get('session_id')
        client_time_spent_in_secs = self.payload.get('client_time_spent_in_secs')
        old_params = self.payload.get('old_params')

        event_services.StateHitEventHandler.record(
            exploration_id, new_state_name, first_time)
        if new_state_name == feconf.END_DEST:
            event_services.MaybeLeaveExplorationEventHandler.record(
                exploration_id, exploration_version, feconf.END_DEST,
                session_id, client_time_spent_in_secs, old_params,
                feconf.PLAY_TYPE_NORMAL)


class ReaderFeedbackHandler(base.BaseHandler):
    """Submits feedback from the reader."""

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    @require_playable
    def post(self, exploration_id):
        """Handles POST requests."""
        state_name = self.payload.get('state_name')
        subject = self.payload.get('subject', 'Feedback from a learner')
        feedback = self.payload.get('feedback')
        include_author = self.payload.get('include_author')

        feedback_services.create_thread(
            exploration_id,
            state_name,
            self.user_id if include_author else None,
            subject,
            feedback)
        self.render_json(self.values)


class ReaderLeaveHandler(base.BaseHandler):
    """Tracks a reader leaving an exploration before completion."""

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    @require_playable
    def post(self, exploration_id, escaped_state_name):
        """Handles POST requests."""
        event_services.MaybeLeaveExplorationEventHandler.record(
            exploration_id,
            self.payload.get('version'),
            self.unescape_state_name(escaped_state_name),
            self.payload.get('session_id'),
            self.payload.get('client_time_spent_in_secs'),
            self.payload.get('params'),
            feconf.PLAY_TYPE_NORMAL)
