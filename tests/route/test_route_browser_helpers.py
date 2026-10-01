from __future__ import annotations
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from route_browser_helpers import reveal_manual_choices


class _Summary:
    def __init__(self):
        self.clicked = 0

    def click(self):
        self.clicked += 1


class _Details:
    def __init__(self, opened=False):
        self.opened = opened
        self.summary = _Summary()
        self.selectors = []

    def get_attribute(self, name):
        if name != 'open':
            raise AssertionError(name)
        return '' if self.opened else None

    def locator(self, selector):
        self.selectors.append(selector)
        if selector != ':scope > summary':
            raise AssertionError(f'expected direct summary selector, got {selector!r}')
        return self.summary


class _Page:
    def __init__(self, details):
        self.details = details
        self.selectors = []

    def locator(self, selector):
        self.selectors.append(selector)
        if selector != '#customizeOrder':
            raise AssertionError(selector)
        return self.details


class RouteBrowserHelperTest(unittest.TestCase):
    def test_closed_manual_choices_clicks_only_direct_summary(self):
        details = _Details(opened=False)
        page = _Page(details)
        reveal_manual_choices(page)
        self.assertEqual(details.selectors, [':scope > summary'])
        self.assertEqual(details.summary.clicked, 1)

    def test_open_manual_choices_does_not_click(self):
        details = _Details(opened=True)
        page = _Page(details)
        reveal_manual_choices(page)
        self.assertEqual(details.selectors, [])
        self.assertEqual(details.summary.clicked, 0)


if __name__ == '__main__':
    unittest.main()
