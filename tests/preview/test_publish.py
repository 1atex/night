import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('night_publish', Path(__file__).resolve().parents[2] / 'scripts/preview/publish.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublicationTests(unittest.TestCase):
    def test_draft_metadata_uses_api_id_instead_of_the_unpublished_tag(self):
        url = 'https://api.github.com/repos/1atex/night/releases/123'
        view = subprocess.CompletedProcess([], 0, json.dumps({'apiUrl': url, 'isDraft': True}), '')
        with patch.object(publisher.subprocess, 'run', return_value=view), patch.object(publisher.subprocess, 'check_output', return_value=json.dumps({'draft': True, 'assets': []})) as fetch:
            self.assertTrue(publisher.metadata('1atex/night', 'v0.18.2-preview.1')['draft'])
            fetch.assert_called_once_with(['gh', 'api', url], text=True)

    def test_publication_rejects_extra_assets_and_wrong_or_missing_hashes(self):
        tag = 'v0.18.2-preview.1'
        prefix = f'https://github.com/1atex/night/releases/download/{tag}/'
        hashes = {'setup.exe': 'a' * 64, 'portable.zip': 'b' * 64}
        release = {'assets': [{'name': name, 'browser_download_url': prefix + name, 'digest': 'sha256:' + digest} for name, digest in hashes.items()]}
        self.assertTrue(publisher.verify_assets(release, '1atex/night', tag, hashes))
        release['assets'].append({'name': 'screenshot.png'})
        self.assertFalse(publisher.verify_assets(release, '1atex/night', tag, hashes))
        release['assets'].pop()
        release['assets'][0]['digest'] = None
        self.assertFalse(publisher.verify_assets(release, '1atex/night', tag, hashes))

    def test_draft_links_change_to_versioned_links_on_publication(self):
        tag = 'v0.18.2-preview.1'
        hashes = {'setup.exe': 'a' * 64, 'portable.zip': 'b' * 64}
        prefix = 'https://github.com/1atex/night/releases/'
        release = {'draft': True, 'html_url': prefix + 'tag/untagged-example', 'assets': [
            {'name': name, 'browser_download_url': prefix + 'download/untagged-example/' + name, 'digest': 'sha256:' + digest}
            for name, digest in hashes.items()
        ]}
        self.assertTrue(publisher.verify_assets(release, '1atex/night', tag, hashes))
        release['draft'] = False
        self.assertFalse(publisher.verify_assets(release, '1atex/night', tag, hashes))
        for asset in release['assets']:
            asset['browser_download_url'] = prefix + 'download/' + tag + '/' + asset['name']
        self.assertTrue(publisher.verify_assets(release, '1atex/night', tag, hashes))
        release['assets'][0]['browser_download_url'] = release['assets'][0]['browser_download_url'].replace('/1atex/night/', '/someone/else/')
        self.assertFalse(publisher.verify_assets(release, '1atex/night', tag, hashes))


if __name__ == '__main__':
    unittest.main()
