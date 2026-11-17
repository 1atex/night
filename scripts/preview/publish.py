import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path


def command(repository, *arguments, check=True):
    result = subprocess.run(['gh', *arguments, '-R', repository], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or 'GitHub publication command failed')
    return result


def metadata(repository, tag):
    result = command(repository, 'release', 'view', tag, '--json', 'apiUrl,isDraft,url', check=False)
    if result.returncode:
        return None
    info = json.loads(result.stdout)
    response = subprocess.check_output(['gh', 'api', info['apiUrl']], text=True)
    return info | json.loads(response)


def verify_assets(release, repository, tag, hashes):
    assets = release.get('assets', [])
    if len(assets) != len(hashes) or {asset['name'] for asset in assets} != set(hashes):
        return False
    prefix = f'https://github.com/{repository}/releases/download/{tag}/'
    if release.get('draft'):
        draft_url = release.get('html_url', '')
        draft_prefix = f'https://github.com/{repository}/releases/tag/'
        draft_tag = draft_url.removeprefix(draft_prefix)
        if draft_url.startswith(draft_prefix) and re.fullmatch(r'untagged-[\w-]+', draft_tag):
            prefix = f'https://github.com/{repository}/releases/download/{draft_tag}/'
    return all(asset.get('browser_download_url') == prefix + asset['name'] and
               asset.get('digest') == 'sha256:' + hashes[asset['name']] for asset in assets)


def publish(repository, tag, assets, notes):
    version = json.loads(Path('package.json').read_text())['version']
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repository) or not re.fullmatch(r'v' + re.escape(version) + r'-preview\.\d+', tag):
        raise ValueError('Release repository or version is invalid')
    names = [f'night-browser-{version}-preview-windows-x64-setup.exe', f'night-browser-{version}-preview-windows-x64.zip']
    if {path.name for path in assets.iterdir()} != set(names):
        raise ValueError('Only the installer and portable ZIP may be published')
    hashes = {}
    for name in names:
        with (assets / name).open('rb') as stream:
            hashes[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    existing = metadata(repository, tag)
    if existing and not existing['draft']:
        if not verify_assets(existing, repository, tag, hashes):
            raise ValueError('Published release differs from the tested files')
        print(existing['html_url'])
        return
    if existing:
        command(repository, 'release', 'upload', tag, *(str(assets / name) for name in names), '--clobber')
        command(repository, 'release', 'edit', tag, '--notes-file', str(notes))
    else:
        command(repository, 'release', 'create', tag, *(str(assets / name) for name in names), '--verify-tag', '--draft', '--prerelease', '--title', f'Night Browser {version}', '--notes-file', str(notes))
    for _ in range(20):
        release = metadata(repository, tag)
        if release and verify_assets(release, repository, tag, hashes):
            break
        time.sleep(1)
    else:
        raise ValueError('GitHub asset digests do not match; release remains a draft')
    command(repository, 'release', 'edit', tag, '--draft=false')
    final = metadata(repository, tag)
    if not final or final['draft'] or not verify_assets(final, repository, tag, hashes):
        raise ValueError('Release verification failed')
    print(final['html_url'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--notes', type=Path, required=True)
    args = parser.parse_args()
    publish(args.repository, args.tag, args.assets, args.notes)
