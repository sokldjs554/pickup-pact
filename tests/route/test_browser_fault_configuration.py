"""Verifier-only ordering regressions; fake page is not browser E2E evidence."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


class Page:
    def __init__(self, response_fault=None, response_status=200):
        self.busy = True
        self.selected = 'none'
        self.sent = []
        self.response_fault = response_fault
        self.response_status = response_status
        self.response = None

    def wait_for_function(self, expression):
        assert 'busy' in expression
        # Completion of the previous request redraws the select from saved state.
        if self.busy:
            self.selected = 'none'
            self.busy = False

    def locator(self, selector):
        return SimpleNamespace(select_option=self.select, click=self.click,
                               input_value=lambda: self.selected)

    def select(self, value):
        self.selected = value

    def click(self):
        # Button waits for the old command, but select_option does not. A click
        # after that redraw therefore submits the restored value, not the draft.
        if self.busy:
            self.wait_for_function('!busy')
        self.sent.append(self.selected)
        body = {'action': 'fault', 'fault': self.selected}
        returned = self.selected if self.response_fault is None else self.response_fault
        self.response = SimpleNamespace(
            status=self.response_status,
            request=SimpleNamespace(method='POST', post_data_json=body),
            url='http://test/api/route/journeys/journey/transfer-controls',
            json=lambda: {'next_transfer_fault': returned},
            text=lambda: 'diagnostic response',
        )

    def expect_response(self, predicate, timeout=10000):
        page = self
        class Wait:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                assert predicate(page.response)
                self.value = page.response
        return Wait()


def fault_function(page):
    source = ast.parse(Path('scripts/verify_handoff_browser.py').read_text())
    node = next(node for node in ast.walk(source)
                if isinstance(node, ast.FunctionDef) and node.name == 'fault')
    def expect(locator):
        class Check:
            def to_be_enabled(self):
                pass
            def to_have_value(self, value):
                assert locator.input_value() == value
        return Check()
    namespace = {'page': page, 'expect': expect}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'fault-helper', 'exec'), namespace)
    return namespace['fault']


def test_previous_response_finishes_before_next_fault_is_selected():
    page = Page()
    fault_function(page)('after_target_hold')
    assert page.sent == ['after_target_hold'], 'old render silently replaced the selected fault'


def test_mismatched_server_fault_is_not_a_valid_recovery_test_setup():
    page = Page(response_fault='none')
    with pytest.raises(AssertionError):
        fault_function(page)('after_target_hold')


def test_failed_control_response_cannot_start_recovery_assertions():
    page = Page(response_status=409)
    with pytest.raises(AssertionError):
        fault_function(page)('after_target_hold')


def test_requested_refusal_uses_the_same_checked_precondition():
    page = Page()
    fault_function(page)('target_reject')
    assert page.sent == ['target_reject']
