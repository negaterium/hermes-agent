import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import daily_umbra_blogger_preflight as preflight
from scripts import daily_umbra_blogger_publish as publisher


class EditorialContextTests(unittest.TestCase):
    def test_recent_post_contains_argument_and_links_not_only_title(self):
        post = {
            'id': 'example',
            'title': 'An ordinary subject',
            'published': '2026-10-01T11:00:00+03:00',
            'content': '<p>An actual argument &amp; detail.</p><p>The ending.</p>'
                       '<p><a href="https://example.org/paper">Source</a></p>',
        }
        summary = preflight._post_summary(post)
        self.assertIn('An actual argument & detail.', summary.get('body_text', ''))
        self.assertIn('The ending.', summary['body_text'])
        self.assertEqual(summary['source_urls'], ['https://example.org/paper'])
        self.assertFalse(summary['body_truncated'])

    def test_review_hold_blocks_before_provider_access(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / 'policy.json'
            policy.write_text(json.dumps({'publication_mode': 'review_only'}))
            output = io.StringIO()
            with patch.object(preflight, '_policy_path', return_value=policy, create=True), \
                 patch.object(preflight, '_load_connector', side_effect=AssertionError('provider must not be accessed')) as connector, \
                 contextlib.redirect_stdout(output):
                self.assertEqual(preflight.main(), 0)
            connector.assert_not_called()
            payload = json.loads(output.getvalue())
            self.assertEqual(payload['status'], 'BLOCKED')
            self.assertEqual(payload['reason'], 'editorial_pilot_review_required')

    def test_publisher_also_blocks_review_hold_before_reading_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / 'policy.json'
            policy.write_text(json.dumps({'publication_mode': 'review_only'}))
            output = io.StringIO()
            with patch.object(preflight, '_policy_path', return_value=policy, create=True), \
                 patch.object(publisher, '_load_connector', side_effect=AssertionError('provider must not be accessed')) as connector, \
                 patch('sys.argv', ['publisher', '--draft-file', str(Path(directory) / 'missing.json')]), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(publisher.main(), 0)
            connector.assert_not_called()
            payload = json.loads(output.getvalue())
            self.assertEqual(payload['status'], 'BLOCKED')
            self.assertEqual(payload['stage'], 'editorial_gate')

    def test_bare_essay_draft_is_rejected_without_editorial_brief(self):
        with tempfile.TemporaryDirectory() as directory:
            draft = Path(directory) / 'draft.json'
            draft.write_text(json.dumps({'title': 'An essay', 'body': '<p>Some prose.</p>'}))
            with self.assertRaisesRegex(ValueError, 'editorial brief'):
                publisher._read_draft(draft)

    def test_unreviewed_or_snippet_evidence_cannot_pass_draft_validation(self):
        brief = {
            'subject': 'A named experiment', 'reader_value': 'An observable result',
            'lane': 'technology', 'format': 'practical_demo',
            'argument': 'A schema can omit fields', 'new_material': 'A runnable comparison',
            'review_passed': True,
            'evidence': [{'method': 'search_snippet', 'reference': 'https://example.org', 'reviewed': True}],
        }
        with tempfile.TemporaryDirectory() as directory:
            draft = Path(directory) / 'draft.json'
            for method, reviewed in [('search_snippet', True), ('full_source_review', False)]:
                with self.subTest(method=method, reviewed=reviewed):
                    brief['evidence'][0].update(method=method, reviewed=reviewed)
                    draft.write_text(json.dumps({'title': 'An article', 'body': '<p>Details.</p>', 'editorial': brief}))
                    with self.assertRaisesRegex(ValueError, 'editorial evidence'):
                        publisher._read_draft(draft)

    def test_evidence_alone_does_not_replace_a_complete_reviewed_brief(self):
        brief = {'evidence': [{'method': 'executed_experiment', 'reference': 'result.json', 'reviewed': True}]}
        with self.assertRaisesRegex(ValueError, 'editorial brief'):
            publisher._validate_editorial(brief)


class EditorialBoundaryTests(unittest.TestCase):
    def test_invalid_policies_fail_before_connector_or_draft_access(self):
        cases = [None, '{', '[]', '{}', '{"publication_mode":false}',
                 '{"publication_mode":"unexpected"}']
        with tempfile.TemporaryDirectory() as directory:
            for index, content in enumerate(cases):
                policy = Path(directory) / f'policy-{index}.json'
                if content is not None:
                    policy.write_text(content, encoding='utf-8')
                for module in (preflight, publisher):
                    with self.subTest(content=content, module=module.__name__):
                        output = io.StringIO()
                        with patch.object(preflight, '_policy_path', return_value=policy, create=True), \
                             patch.object(module, '_load_connector') as connector, \
                             patch.object(publisher, '_read_draft') as draft, \
                             patch('sys.argv', ['publisher', '--draft-file', str(Path(directory) / 'missing.json')]), \
                             contextlib.redirect_stdout(output):
                            self.assertEqual(module.main(), 0)
                        connector.assert_not_called()
                        draft.assert_not_called()
                        payload = json.loads(output.getvalue())
                        self.assertEqual(payload['status'], 'FAILED')
                        self.assertEqual(payload['stage'], 'preflight' if module is preflight else 'editorial_gate')

    def test_paths_follow_profile_switch_a_b_a(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = Path(directory) / 'a', Path(directory) / 'b'
            for home, mode in ((a, 'review_only'), (b, 'live')):
                policy = home / 'state' / 'blog-editorial-reset' / 'publication-policy.json'
                policy.parent.mkdir(parents=True)
                policy.write_text(json.dumps({'publication_mode': mode}), encoding='utf-8')
                record = home / 'state' / 'umbra-blogger' / '2026-10-01-test.json'
                record.parent.mkdir(parents=True)
                record.write_text(json.dumps({'state': 'completed', 'provider_id': home.name}), encoding='utf-8')
            for home, mode in ((a, 'review_only'), (b, 'live'), (a, 'review_only')):
                with patch.dict('os.environ', {'HERMES_HOME': str(home)}):
                    self.assertEqual(preflight._publication_mode(), mode)
                    prior = publisher._prior_ambiguous_run('2026-10-01')
                    self.assertIsNotNone(prior)
                    assert prior is not None
                    self.assertEqual(prior['provider_id'], home.name)

    def test_long_body_preserves_opening_and_ending_and_marks_omission(self):
        summary = preflight._post_summary({'content': '<p>OPENING ' + 'x' * 10000 + ' ENDING</p>'})
        self.assertTrue(summary['body_truncated'])
        self.assertTrue(summary['body_text'].startswith('OPENING'))
        self.assertTrue(summary['body_text'].endswith('ENDING'))
        self.assertIn('[body excerpt omitted]', summary['body_text'])
        self.assertLess(len(summary['body_text']), 6600)

    def test_valid_brief_acceptance_and_literal_review_boolean(self):
        brief = {'subject': 'Synthetic example', 'reader_value': 'A useful comparison',
                 'lane': 'technology', 'format': 'demonstration', 'argument': 'Fields can be lost',
                 'new_material': 'Saved executed results', 'review_passed': True,
                 'evidence': [{'method': 'executed_experiment', 'reference': 'result.json', 'reviewed': True}]}
        with tempfile.TemporaryDirectory() as directory:
            draft = Path(directory) / 'draft.json'
            draft.write_text(json.dumps({'title': 'Example', 'body': '<p>Details.</p>', 'editorial': brief}), encoding='utf-8')
            title, body, payload = publisher._read_draft(draft)
            self.assertEqual((title, body), ('Example', '<p>Details.</p>'))
            self.assertEqual(payload['editorial'], brief)
        for value in (False, 'true', 1):
            brief['review_passed'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'substantive review'):
                publisher._validate_editorial(brief)


if __name__ == '__main__':
    unittest.main()
