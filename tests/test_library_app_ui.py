"""Actual Studio bindings preview approved references without promoting versions."""
import importlib.util
import json
import os
import unittest
from unittest.mock import patch

from tests import test_library_controller, test_processing_app_ui


@unittest.skipUnless(importlib.util.find_spec('gradio'), 'Gradio is not installed')
class LibraryAppUITests(unittest.TestCase):
    def setUp(self):
        test_library_controller.LibraryControllerTests.setUp(self)
        published = self.library.create(self.source, 'Chair')
        self.library_asset = published['asset']['id']
        self.library_version = published['version']['id']
        self.environment = patch.dict(os.environ, {'GRADIO_ANALYTICS_ENABLED': 'False', 'PATH': os.defpath})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.namespace = test_processing_app_ui.build_studio_ui(self.fixture)

    def test_actual_library_controls_and_version_preview(self):
        labels = [c.get('props', {}).get('value') for c in self.namespace['app'].config['components']]
        for label in ('Create New Asset', 'Set Current Version', 'Open Source Review', 'Restore Archived Asset'):
            self.assertIn(label, labels)
        previous = self.store.get_library_asset(self.library_asset)['current_version_id']
        message, accordion = self.namespace['library_view_version_action'](self.library_asset, self.library_version)
        self.assertIn('Chair · v001', message)
        self.assertTrue(accordion['open'])
        payload = json.loads(self.namespace['VIEWER_STATE'].read_text(encoding="utf-8"))
        self.assertEqual(payload['assetName'], 'Chair')
        self.assertEqual(payload['models'][0]['label'], 'Chair · v001')
        self.assertEqual(self.store.get_library_asset(self.library_asset)['current_version_id'], previous)

    def test_source_review_navigation_and_missing_batch_notice(self):
        tab, project = self.namespace['library_open_source_review'](self.library_asset, self.library_version)
        self.assertEqual(tab['selected'], 'review')
        self.assertEqual(project, self.fixture.project)
        details = self.namespace['library_source_review_details'](self.library_asset, self.library_version)
        self.assertEqual(details[1]['value'], self.source)
        _, notice = self.namespace['library_open_source_batch'](self.library_asset, self.library_version)
        self.assertIn('without a persisted batch', notice)
